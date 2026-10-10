from dataclasses import replace
from pathlib import Path
import json
import random

import httpx
import pytest

from app.config import Config, BackendSettings, load_config
from app.core.contracts import MoveResult
from app.core.move_sampling import select_move
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.opponents import HTTPOpponent, Unsupported, parse_move
from scripts.compare_models import run_case, summarize


def position():
    game = TicTacToe()
    state = game.apply_action(game.initial_state(), 'place_A1')
    return game, state, game.decision_request(state, game.legal_actions(state))


def settings(protocol='chat'):
    return BackendSettings(backend='ollama', protocol=protocol, model='test', input_mode='text', max_choices=26)


def test_chat_output_budget_is_configured_recorded_and_truncation_is_explicit():
    from app.inference.opponents import role_metadata
    config = load_config(Path('configs/models/qwen3-vl.yaml'))
    _, _, request = position()
    assert HTTPOpponent(config)._payload(request, None)['options']['num_predict'] == 512
    assert role_metadata(config)['opponent']['generation_max_tokens'] == 512
    assert 'generation_max_tokens' not in role_metadata(Config())['opponent']
    result = parse_move({'model': config.opponent_settings.model, 'done_reason': 'length',
                         'message': {'content': '', 'thinking': 'unfinished reasoning'}},
                        request, config.opponent_settings)
    assert 'Truncated opponent response' in result.rejection


@pytest.mark.parametrize('budget', [0, -1, True, 1.5])
def test_invalid_chat_output_budget_is_rejected(tmp_path, budget):
    path = tmp_path / 'config.yaml'
    path.write_text('opponent:\n  backend: ollama\n  protocol: chat\n  model: test\n'
                    f'  generation_max_tokens: {json.dumps(budget)}\n')
    with pytest.raises(ValueError, match='positive integer'):
        load_config(path)


def test_chat_move_requires_no_imajev_uncertainty_fields():
    game, state, request = position()
    result = parse_move({'model': 'test', 'message': {'content': '{"choice":"place_B1"}'}}, request, settings())
    assert game.decode_decision(state, result) == 'place_B1'
    assert result.probabilities is result.abstained is result.unknown_probability is None
    assert select_move(game, state, result, 0, random.Random(1))[:2] == ('place_B1', 'place_B1')
    with pytest.raises(ValueError, match='no move distribution'):
        select_move(game, state, result, 1, random.Random(1))


@pytest.mark.parametrize('content', ['not JSON', '{"choice":"place_A1"}', '{"choice":3}'])
def test_invalid_chat_proposal_is_retained_and_rejected(content):
    game, state, request = position()
    result = parse_move({'model': 'test', 'message': {'content': content}}, request, settings())
    assert result.raw['message']['content'] == content
    with pytest.raises(ValueError):
        game.decode_decision(state, result)


def test_native_optional_distribution_validation():
    _, _, request = position()
    raw = {'model': 'test', 'answers': {'move': {'type': 'choice', 'choice': 'place_B1', 'confidence': .8}}}
    result = parse_move(raw, request, settings('systemone'))
    assert result.confidence == .8 and result.probabilities is None and result.rejection is None
    raw['answers']['move']['probabilities'] = {'place_B1': float('nan')}
    assert 'distribution' in parse_move(raw, request, settings('systemone')).rejection
    raw['answers'] = []
    assert parse_move(raw, request, settings('systemone')).rejection


def test_full_choice_set_is_never_pruned():
    backend = HTTPOpponent(Config(opponent=settings('systemone')))
    request = {'questions': {'move': {'criteria': {str(n): None for n in range(27)}}}}
    with pytest.raises(Unsupported, match='all 27'):
        backend.preflight(request, None)
    assert len(request['questions']['move']['criteria']) == 27


def test_native_images_use_runtime_wire_formats():
    _, _, request = position()
    config = Config(opponent=replace(settings('systemone'), input_mode='text_image'))
    ollama = HTTPOpponent(config)._payload(request, b'png')
    cpp = HTTPOpponent(replace(config, opponent=replace(config.opponent, backend='llama_cpp')))._payload(request, b'png')
    assert ollama['images'] == ['cG5n']
    assert cpp['images'] == ['data:image/png;base64,cG5n']


def test_profiles_load_and_keep_names():
    from pathlib import Path
    profiles = list(Path('configs/models').glob('*.yaml'))
    assert len(profiles) == 16
    for profile in profiles:
        config = load_config(profile)
        assert config.expected_model == 'imajev-4b-nf4'
        assert config.move_temperature == 0


def test_comparison_counts_unsupported_in_coverage_and_keeps_invalid_proposal():
    game, state, request = position()
    case = {'id': '0:canonical', 'state': game.encode_state(state), 'request': request,
            'request_sha256': 'request', 'image_sha256': 'image', 'order': 'canonical'}
    class Invalid:
        def choose_move(self, request, png):
            return MoveResult('illegal', 'test', {'original': True}, 'ollama')
    row = run_case(game, Config(), Invalid(), case, b'', 42)
    assert row['raw_proposal'] == 'illegal' and row['raw_response'] == {'original': True}
    summary = summarize([row, {**row, 'status': 'unsupported'}])
    assert summary['coverage'] == .5
    assert summary['valid_rate_all'] == 0
    assert summary['statuses']['unsupported'] == 1


def test_http_chat_transport_and_timeout_cannot_overlap(monkeypatch):
    import httpx
    import app.inference.opponents as adapters
    _, _, request = position()
    calls = []
    real_client = httpx.Client
    def handler(req):
        calls.append(json.loads(req.content))
        raise httpx.ReadTimeout('slow', request=req)
    monkeypatch.setattr(adapters.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    backend = HTTPOpponent(Config(opponent=settings(), external_inference=True))
    from app.inference.imajev_client import InferenceError
    with pytest.raises(InferenceError, match='timed out'):
        backend.choose_move(request)
    with pytest.raises(InferenceError, match='may still be working'):
        backend.choose_move(request)
    assert len(calls) == 1
    assert calls[0]['options']['temperature'] == 0
    assert calls[0]['format']['properties']['choice']['enum'] == list(request['questions']['move']['criteria'])


def test_managed_activation_and_inference_times_are_separate(monkeypatch):
    import app.inference.service_manager as services
    from app.inference.service_manager import ServiceManager
    trace = []
    manager = ServiceManager(Config(opponent=settings()))
    monkeypatch.setattr(manager, '_activate_ollama', lambda s: trace.append('activate'))
    monkeypatch.setattr(manager, '_release_opponent', lambda: trace.append('release'))
    ticks = iter([0, 3, 3, 8, 10, 10, 10, 12])
    monkeypatch.setattr(services.time, 'monotonic', lambda: next(ticks))
    operation = lambda: MoveResult('a', 'test', {})
    result = manager.run_opponent(settings(), operation)
    assert result.raw['activation_seconds'] == 3
    assert result.raw['inference_seconds'] == 5
    result = manager.run_opponent(settings(), operation)
    assert result.raw['activation_seconds'] == 0 and result.raw['inference_seconds'] == 2
    assert trace == ['release', 'activate']


@pytest.mark.parametrize('backend', ['ollama', 'llama_cpp', 'decider'])
def test_failed_gpu_release_blocks_next_activation(monkeypatch, backend):
    from app.inference.service_manager import ServiceManager
    manager = ServiceManager(Config(opponent=settings()))
    manager.opponent_ollama = ('http://127.0.0.1:11434', 'owned')
    def release():
        raise RuntimeError('Unload unconfirmed')
    monkeypatch.setattr(manager, '_release_opponent', release)
    monkeypatch.setattr(manager, '_activate_ollama', lambda s: pytest.fail('Activated despite pending unload'))
    monkeypatch.setattr(manager, '_activate_local', lambda s: pytest.fail('Activated despite pending unload'))
    with pytest.raises(RuntimeError, match='unconfirmed'):
        manager.run_opponent(replace(settings(), backend=backend), lambda: pytest.fail('Sent overlapping inference'))
    assert manager.opponent_ollama[1] == 'owned'


@pytest.mark.parametrize('source', ['ollama', 'llama_cpp', 'decider'])
@pytest.mark.parametrize('destination', ['ollama', 'llama_cpp', 'decider'])
def test_failed_model_activation_never_sends_inference(monkeypatch, source, destination):
    from app.inference.service_manager import ServiceManager
    manager = ServiceManager(Config())
    manager.active_opponent = replace(settings(), backend=source, model='previous-model')
    released = False

    def release():
        nonlocal released
        released = True
        manager.active_opponent = None

    def activate(_):
        assert released, 'Activation began before previous model release'
        raise RuntimeError('Expected model not ready')

    monkeypatch.setattr(manager, '_release_opponent', release)
    monkeypatch.setattr(manager, '_activate_ollama', activate)
    monkeypatch.setattr(manager, '_activate_local', activate)
    with pytest.raises(RuntimeError, match='not ready'):
        manager.run_opponent(replace(settings(), backend=destination, model='next-model'),
                             lambda: pytest.fail('Inference sent despite failed activation'))
    assert manager.active_opponent is None


@pytest.mark.parametrize('protocol', ['chat', 'systemone'])
@pytest.mark.parametrize('wrong_digest', [False, True])
def test_ollama_activation_checks_loaded_identity_before_inference(monkeypatch, protocol, wrong_digest):
    from app.inference.service_manager import ServiceManager
    calls = []
    unloaded = []
    entry = {'name': 'test', 'digest': 'installed-digest'}
    manager = ServiceManager(Config())
    monkeypatch.setattr(manager, '_ensure_ollama', lambda _: None)
    monkeypatch.setattr(manager, 'stop', lambda: calls.append('previous process exited'))
    monkeypatch.setattr(manager, '_release_opponent', lambda: unloaded.append(True))
    real_client = httpx.Client

    def handle(req):
        calls.append(req.url.path)
        if req.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [entry]})
        if req.url.path == '/api/ps':
            resident = [] if calls.count('/api/ps') == 1 else [{**entry, 'digest': 'wrong' if wrong_digest else entry['digest']}]
            return httpx.Response(200, json={'models': resident})
        if req.url.path == '/api/version':
            return httpx.Response(200, json={'version': '0.40.2'})
        if req.url.path == '/api/show':
            return httpx.Response(200, json={'capabilities': ['completion']})
        assert 'previous process exited' in calls
        if req.url.path == '/api/chat':
            assert json.loads(req.content)['messages'] == []
            return httpx.Response(200, json={'model': 'test', 'done': True})
        assert req.url.path == '/v1/systemone'
        return httpx.Response(200, json={'model': 'test', 'answers': {
            'ready': {'type': 'choice', 'choice': 'ready'}}})

    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    if wrong_digest:
        with pytest.raises(RuntimeError, match='identity'):
            manager.run_opponent(settings(protocol), lambda: pytest.fail('Inference sent to an unverified model'))
        assert unloaded
    else:
        manager.run_opponent(settings(protocol), lambda: MoveResult('a', 'test', {}))
        assert manager.runtime_provenance['served_model']['digest'] == 'installed-digest'


@pytest.mark.parametrize('backend', ['llama_cpp', 'decider'])
@pytest.mark.parametrize('wrong_model', [False, True])
def test_local_activation_waits_for_health_and_checks_identity(monkeypatch, tmp_path, backend, wrong_model):
    import sys
    from types import SimpleNamespace
    from app.inference.service_manager import ServiceManager
    trace = []
    model = tmp_path / 'model.gguf'
    model.write_bytes(b'fixture weights')
    selected = replace(settings('systemone'), backend=backend, executable=sys.executable,
                       model_path=str(model), endpoint='http://127.0.0.1:8080/v1/systemone')
    manager = ServiceManager(Config())
    monkeypatch.setattr(manager, 'stop', lambda: trace.append('previous exited'))

    def release():
        manager.opponent_process = None
        manager.active_opponent = None
        if manager.opponent_log:
            manager.opponent_log.close()
            manager.opponent_log = None

    monkeypatch.setattr(manager, '_release_opponent', release)
    probe = SimpleNamespace(connect_ex=lambda _: 1)
    class Probe:
        def __enter__(self): return probe
        def __exit__(self, *_): pass
    monkeypatch.setattr('app.inference.service_manager.socket.socket', lambda: Probe())
    monkeypatch.setattr('app.inference.service_manager.subprocess.check_output', lambda *a, **kw: 'test runtime')

    def spawn(*args, **kwargs):
        assert trace == ['previous exited']
        trace.append('spawned')
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr('app.inference.service_manager.subprocess.Popen', spawn)
    monkeypatch.setattr('app.inference.service_manager.time.sleep', lambda _: None)
    real_client = httpx.Client

    def handle(req):
        trace.append(req.url.path)
        if req.url.path == '/health':
            return httpx.Response(503 if trace.count('/health') == 1 else 200)
        assert trace.count('/health') == 2
        return httpx.Response(200, json={'data': [{'id': 'wrong' if wrong_model else 'test'}]})

    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    try:
        if wrong_model:
            with pytest.raises(RuntimeError, match='identity'):
                manager.run_opponent(selected, lambda: pytest.fail('Inference sent to wrong model'))
            assert manager.active_opponent is None
        else:
            manager.run_opponent(selected, lambda: trace.append('inference') or MoveResult('a', 'test', {}))
            assert trace == ['previous exited', 'spawned', '/health', '/health', '/v1/models', 'inference']
    finally:
        release()


def test_managed_opponent_timeout_releases_owned_runtime(monkeypatch):
    from app.inference.service_manager import ServiceManager
    from app.inference.imajev_client import InferenceError
    manager = ServiceManager(Config(opponent=settings()))
    trace = []
    monkeypatch.setattr(manager, '_release_opponent', lambda: trace.append('release'))
    monkeypatch.setattr(manager, '_activate_ollama', lambda s: trace.append('activate'))
    def timeout():
        raise InferenceError('timed out')
    with pytest.raises(InferenceError):
        manager.run_opponent(settings(), timeout)
    assert trace == ['release', 'activate', 'release']


def test_comparison_freeze_and_resume_reject_prompt_tampering(qapp, monkeypatch, tmp_path):
    import sys
    import scripts.compare_models as comparison
    config = tmp_path / 'config.yaml'
    config.write_text('opponent:\n  backend: ollama\n  protocol: chat\n  model: test\n')
    output = tmp_path / 'experiment'
    argv = ['compare', '--config', str(config), '--limit', '1', '--output', str(output), '--prepare-only']
    monkeypatch.setattr(sys, 'argv', argv)
    comparison.main()
    cases = json.loads((output / 'cases.json').read_text())
    assert len(cases) == 3
    assert {r['order'] for r in cases} == {'canonical', 'reverse', 'shuffle'}
    monkeypatch.setattr(sys, 'argv', argv + ['--resume'])
    comparison.main()
    cases[0]['request']['state'] = 'Changed board'
    (output / 'cases.json').write_text(json.dumps(cases))
    with pytest.raises(SystemExit):
        comparison.main()


@pytest.mark.parametrize('unsupported', [False, True])
def test_session_uses_separate_opponent_without_probability_fields(qapp, tmp_path, unsupported):
    from app.core.session import SessionController
    from app.ui.window import Window
    from test_session import Fake, wait_for
    from test_protocol import INK
    recognition = Fake()
    class Opponent:
        def choose_move(self, request, image):
            if unsupported:
                raise Unsupported('Complete action set is unsupported')
            return MoveResult(next(iter(request['questions']['move']['criteria'])), 'test', {}, 'ollama')
    config = Config(opponent=settings(), learning_directory=tmp_path / 'learning')
    controller = SessionController(TicTacToe(), recognition, config, opponent=Opponent())
    controller.ready = True
    controller.new_game()
    window = Window(controller)
    try:
        controller.set_pending(INK)
        controller.submit()
        wait_for(qapp, lambda: controller.phase == ('error' if unsupported else 'human') and not controller.busy)
        assert len(recognition.calls) == 1
        assert 'Recognition: imajev-4b-nf4' in window.model.text()
        assert 'Opponent: test' in window.model.text()
        assert not window.move_temperature.isEnabled()
        assert controller.state.revision == (1 if unsupported else 2)
        assert controller.termination is None
        if not unsupported:
            assert controller.events[-1]['move_result']['probabilities'] is None
            assert controller.events[-1]['model_proposed_choice']
    finally:
        window.close()
