from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.config import Config, load_config
from app.core.contracts import MoveResult
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.imajev_client import InferenceError
from app.inference.native import Unsupported
from app.inference.opponents import HTTPOpponent, role_metadata
from app.inference.recognition import create_recognizer, parse_recognition
from app.inference.service_manager import ServiceManager, runtime_key
from test_protocol import INK, answer
from test_session import wait_for


PROFILE = Path('configs/liquid-d1-3b-shared.yaml')


def native_reply(request, choices=None, probability=1):
    choices = choices or {'symbol': 'X', 'cell': 'A1'}
    raw = answer(request, choices)
    raw['model'] = 'liquid-d1-3b-q8'
    for name, value in raw['answers'].items():
        value.pop('unknown_probability')
        value.pop('abstained')
        count = len(value['probabilities']) - 1
        value['probabilities'] = {key: probability if key == choices[name] else (1 - probability) / count
                                  for key in value['probabilities']}
    return raw


def request():
    game = TicTacToe()
    return game.recognition_request(game.initial_state(), INK)


def test_native_scores_and_missing_uncertainty_are_preserved():
    config = load_config(PROFILE)
    result = parse_recognition(native_reply(request(), probability=.9), request(), config.recognition_settings)
    assert result.score_kind == 'choice_probability' and result.score('symbol') == .9
    assert result.answers['symbol'].unknown_probability is None
    assert result.answers['symbol'].abstained is None
    assert result.answers['symbol'].confidence is None
    game = TicTacToe()
    assert game.decode_recognition(game.initial_state(), INK, result, .85) == 'place_A1'
    with pytest.raises(ValueError, match='90.0%'):
        game.decode_recognition(game.initial_state(), INK, result, .95)


@pytest.mark.parametrize('field,value', [
    ('probabilities', None), ('probabilities', {}),
    ('probabilities', {'X': float('nan'), 'O': 0, 'invalid': 0}),
    ('probabilities', {'X': True, 'O': 0, 'invalid': 0}),
    ('probabilities', {'X': .2, 'O': .7, 'invalid': .1}),
    ('probabilities', {'X': .7, 'O': .1, 'invalid': .1}),
    ('choice', 'unoffered'), ('choice', []), ('type', 'noul'),
    ('confidence', 2), ('confidence', True), ('unknown_probability', -1), ('abstained', 1),
])
def test_native_malformed_answers_retain_raw(field, value):
    config = load_config(PROFILE)
    raw = native_reply(request())
    raw['answers']['symbol'][field] = value
    with pytest.raises(InferenceError) as error:
        parse_recognition(raw, request(), config.recognition_settings)
    assert error.value.raw is raw


@pytest.mark.parametrize('change', ['model', 'missing_cell', 'answers'])
def test_native_requires_every_answer_and_model_identity(change):
    raw = native_reply(request())
    if change == 'model': raw['model'] = 'wrong'
    elif change == 'missing_cell': raw['answers'].pop('cell')
    else: raw['answers'] = []
    with pytest.raises(InferenceError):
        parse_recognition(raw, request(), load_config(PROFILE).recognition_settings)


def test_recognition_preflight_does_not_prune_or_activate(monkeypatch):
    config = load_config(PROFILE)
    manager = ServiceManager(config)
    monkeypatch.setattr(manager, 'run_backend', lambda *_: pytest.fail('GPU activated despite failed preflight'))
    recognizer = create_recognizer(replace(config, recognition=replace(config.recognition, max_choices=3)), manager=manager)
    with pytest.raises(Unsupported, match='all 10'):
        recognizer.recognize(request(), b'png')
    with pytest.raises(Unsupported, match='image'):
        recognizer.recognize(request(), None)
    assert len(request()['questions']['cell']['criteria']) == 10


@pytest.mark.parametrize('backend,protocol,mode', [('decider', 'systemone', 'text'), ('ollama', 'chat', 'text_image'), ('llama_cpp', 'systemone', 'text')])
def test_nonvision_recognition_configuration_is_rejected(tmp_path, backend, protocol, mode):
    config = tmp_path / 'config.yaml'
    config.write_text(f'recognition:\n  backend: {backend}\n  protocol: {protocol}\n  model: test\n  input_mode: {mode}\nopponent:\n  backend: imajev\n  model: imajev-2b\n')
    with pytest.raises(ValueError, match='image-capable'):
        load_config(config)


def test_native_threshold_policy_and_coaching_are_explicit(tmp_path):
    import yaml
    data = yaml.safe_load(PROFILE.read_text())
    data['learning'] = {'coach_backend': 'shared'}
    path = tmp_path / 'config.yaml'
    data['recognition']['min_effective_probability'] = .85
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match='min_choice_probability'):
        load_config(path)
    data['recognition'].pop('min_effective_probability')
    data['recognition']['min_choice_probability'] = .9
    data['opponent']['prompt_variant'] = 'coached_quoted'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match='coach_backend: ollama'):
        load_config(path)
    data['learning'] = {'coach_backend': 'ollama', 'model': 'qwen3:8b'}
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).threshold == .9


def test_same_runtime_for_different_roles_and_ordered_asset_switch(monkeypatch):
    config = load_config(PROFILE)
    manager = ServiceManager(config)
    calls = []
    real_client = httpx.Client

    def activate(settings):
        calls.append(('activate', settings.projector_path))
        manager.opponent_process = SimpleNamespace(pid=123, poll=lambda: None)
    def release():
        calls.append(('release',))
        manager.active_opponent = manager.opponent_process = None
    def handle(req):
        if req.method == 'GET':
            if req.url.path == '/health': return httpx.Response(200)
            return httpx.Response(200, json={'data': [{'id': config.recognition.model, 'architecture': {
                'input_modalities': ['text', 'image'], 'output_modalities': ['decisions']}}]})
        body = json.loads(req.content)
        calls.append(('request', 'images' in body))
        choices = {'move': next(iter(body['questions']['move']['criteria']))} if 'move' in body['questions'] else None
        return httpx.Response(200, json=native_reply(body, choices))
    monkeypatch.setattr(manager, '_activate_local', activate)
    monkeypatch.setattr(manager, '_release_opponent', release)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    recognizer = create_recognizer(config, manager=manager)
    opponent = HTTPOpponent(config, manager)
    first = recognizer.warmup(request(), b'png')
    game = TicTacToe()
    state = game.apply_action(game.initial_state(), 'place_A1')
    middle = opponent.choose_move(game.decision_request(state, game.legal_actions(state)), b'png')
    last = recognizer.recognize(request(), b'png')
    assert [first.raw['runtime_reused'], middle.raw['runtime_reused'], last.raw['runtime_reused']] == [False, True, True]
    assert {r.raw['runtime_pid'] for r in (first, middle, last)} == {123}
    assert [row for row in calls if row[0] == 'request'] == [('request', True), ('request', False), ('request', True)]
    assert sum(row[0] == 'activate' for row in calls) == 1
    different = replace(config.recognition, projector_path='different.gguf')
    manager.run_backend(different, lambda: last)
    assert calls[-2:] == [('release',), ('activate', 'different.gguf')]
    assert runtime_key(config.recognition) == runtime_key(config.opponent)
    assert runtime_key(config.recognition) != runtime_key(replace(config.recognition, context_limit=4096))


def test_reuse_requires_current_identity_and_image_capability(monkeypatch):
    config = load_config(PROFILE)
    manager = ServiceManager(config)
    manager.active_opponent = config.opponent
    released = []
    monkeypatch.setattr(manager, '_release_opponent', lambda: released.append(True))
    real_client = httpx.Client
    def handle(req):
        return httpx.Response(200, json={'data': [{'id': config.recognition.model, 'architecture': {
            'input_modalities': ['text'], 'output_modalities': ['decisions']}}]})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(RuntimeError, match='image decisions'):
        manager.run_backend(config.recognition, lambda: pytest.fail('inference before capability check'))
    assert released == [True]


@pytest.mark.parametrize('external', [False, True])
def test_timeout_requires_release_or_external_restart(monkeypatch, external):
    config = replace(load_config(PROFILE), external_inference=external)
    manager = ServiceManager(config)
    real_client = httpx.Client
    attempts, released = [], []
    monkeypatch.setattr(manager, '_activate_local', lambda _: None)
    def release():
        released.append(True)
        manager.active_opponent = None
    monkeypatch.setattr(manager, '_release_opponent', release)
    def handle(req):
        attempts.append(True)
        raise httpx.ReadTimeout('slow', request=req)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    recognizer = create_recognizer(config, manager=manager)
    for iteration in range(2):
        with pytest.raises(InferenceError, match='timed out|may still be working'):
            recognizer.recognize(request(), b'png')
    assert len(attempts) == (1 if external else 2)
    assert bool(released) != external


def test_gpu_gate_serializes_entire_operations(monkeypatch):
    config = load_config(PROFILE)
    manager = ServiceManager(config)
    monkeypatch.setattr(manager, '_activate_local', lambda _: None)
    monkeypatch.setattr(manager, '_verify_active', lambda _: None)
    entered, release, attempted, second_entered = (threading.Event() for _ in range(4))
    errors = []
    def operation(first):
        (entered if first else second_entered).set()
        if first: assert release.wait(3)
        return MoveResult('a', 'test', {})
    def call(first):
        if not first: attempted.set()
        try: manager.run_backend(config.recognition, lambda: operation(first))
        except BaseException as exc: errors.append(exc)
    first = threading.Thread(target=call, args=(True,))
    second = threading.Thread(target=call, args=(False,))
    first.start()
    assert entered.wait(3)
    second.start()
    assert attempted.wait(3)
    assert not second_entered.is_set()
    release.set()
    first.join(3); second.join(3)
    assert second_entered.is_set() and not errors


def test_native_session_records_policy_and_rejects_mismatched_restore(qapp, tmp_path):
    config = load_config(PROFILE)
    c = SessionController(TicTacToe(), create_recognizer(config), config)
    record = c.record()
    assert record['inference']['recognition']['score_kind'] == 'choice_probability'
    assert record['inference']['recognition']['projector_path'] == config.recognition.projector_path
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(record))
    c.restore(path)
    other = replace(config, threshold=.9)
    c = SessionController(TicTacToe(), create_recognizer(other), other)
    with pytest.raises(ValueError, match='roles differ'):
        c.restore(path)
    assert role_metadata(Config())['version'] == 1


def test_failed_recognition_keeps_raw_response_and_ink(qapp, monkeypatch):
    config = load_config(PROFILE)
    recognizer = create_recognizer(replace(config, external_inference=True))
    raw = native_reply(request())
    raw['answers'].pop('cell')
    monkeypatch.setattr('app.inference.recognition.request_json', lambda *_: raw)
    c = SessionController(TicTacToe(), recognizer, config)
    c.ready = True
    c.new_game()
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: not c.busy)
    assert c.phase == 'error' and c.state.revision == 0 and c.pending == INK
    assert c.events[-1]['reply'] == raw


def test_native_controller_startup_and_complete_turn_share_runtime(qapp, monkeypatch):
    from app.ui.window import Window
    config = replace(load_config(PROFILE), coach_backend='shared', coach_model='')
    manager = ServiceManager(config)
    activations = []

    def activate(settings):
        activations.append(settings.model)
        manager.opponent_process = SimpleNamespace(pid=321, poll=lambda: None)

    monkeypatch.setattr(manager, '_activate_local', activate)
    monkeypatch.setattr(manager, '_verify_active', lambda _: None)
    monkeypatch.setattr(manager, 'start', lambda: pytest.fail('Native startup activated ImaJEV'))

    def reply(endpoint, payload, timeout):
        choices = ({'move': next(key for key in payload['questions']['move']['criteria']
                                if key.removeprefix('place_') == 'B2')}
                   if 'move' in payload['questions'] else None)
        return native_reply(payload, choices)

    monkeypatch.setattr('app.inference.recognition.request_json', reply)
    monkeypatch.setattr('app.inference.opponents.request_json', reply)
    c = SessionController(TicTacToe(), create_recognizer(config), config)
    c.service_manager = manager
    c.start()
    wait_for(qapp, lambda: c.ready and not c.busy)
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: c.state.revision == 2 and not c.busy)
    assert c.phase == 'human' and c.state.board[:2] == ('X', '') and c.state.board[4] == 'O'
    assert activations == ['liquid-d1-3b-q8']
    assert [event['reply']['runtime_reused'] for event in c.events] == [False, True, True]
    window = Window(c)
    window.refresh()
    assert 'Recognition: liquid-d1-3b-q8' in window.model.text()
    assert 'Recognition: liquid-d1-3b-q8 · llama_cpp' in window.diag.toPlainText()
    coached_index = window.prompt_selector.findData('coached_quoted')
    assert coached_index >= 0
    assert not window.prompt_selector.model().item(coached_index).isEnabled()
    assert 'learning.coach_backend: ollama' in window.prompt_selector.model().item(coached_index).toolTip()
    assert 'Coached quoted unavailable: configure an Ollama coach.' in window.prompt_availability.text()
    assert not window.prompt_availability.isHidden()
    assert 'Runtime: reused' in c.diagnostics
    c.shutdown_done = True
    window.close()


@pytest.mark.parametrize('native,coach_backend,enabled', [
    (True, 'shared', False), (True, 'ollama', True), (False, 'shared', True),
])
def test_coached_mode_availability_survives_game_changes(qapp, monkeypatch, native, coach_backend, enabled):
    from app.ui.window import Window
    from test_session import Fake
    config = load_config(PROFILE) if native else Config()
    config = replace(config, coach_backend=coach_backend,
                     coach_model='qwen3.6:latest' if coach_backend == 'ollama' else '')
    c = SessionController(TicTacToe(), Fake(), config)
    monkeypatch.setattr(c, 'start', lambda: None)
    c.ready = True
    c.new_game()
    window = Window(c)
    for game_id in ('tic_tac_toe', 'boku', 'tic_tac_toe'):
        window.selector.setCurrentIndex(window.selector.findData(game_id))
        assert c.game.id == game_id
        index = window.prompt_selector.findData('coached_quoted')
        assert index >= 0
        assert window.prompt_selector.model().item(index).isEnabled() == enabled
        assert window.prompt_availability.isHidden() == enabled
        for mode in ('legacy', 'quoted'):
            index = window.prompt_selector.findData(mode)
            assert window.prompt_selector.model().item(index).isEnabled()
            window.prompt_selector.setCurrentIndex(index)
            assert c.config.prompt_variant == mode
    window.close()


def test_legacy_imajev_role_metadata_still_restores(qapp, tmp_path):
    from test_session import Fake
    config = Config()
    c = SessionController(TicTacToe(), Fake(), config)
    record = c.record()
    assert record['inference'] == {'version': 1,
        'recognition': {'backend': 'imajev', 'model': config.expected_model, 'endpoint': config.endpoint},
        'opponent': role_metadata(config)['opponent']}
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(record))
    c.restore(path)
    record.pop('inference')
    path.write_text(json.dumps(record))
    c.restore(path)


def test_projector_install_verifies_download_and_skips_verified_asset(monkeypatch, tmp_path):
    import hashlib
    from contextlib import contextmanager
    from scripts import install_liquid_d1 as installer
    data = b'local projector fixture'
    monkeypatch.setattr(installer, 'SHA256', hashlib.sha256(data).hexdigest())
    downloads = []

    @contextmanager
    def stream(*args, **kwargs):
        downloads.append(args)
        yield SimpleNamespace(raise_for_status=lambda: None, iter_bytes=lambda _: iter([data]))

    monkeypatch.setattr(installer.httpx, 'stream', stream)
    destination = installer.install(tmp_path)
    assert destination.read_bytes() == data
    installer.install(tmp_path)
    assert len(downloads) == 1 and not list(tmp_path.glob('*.part'))


def test_projector_install_rejects_corruption_without_overwriting(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from scripts import install_liquid_d1 as installer

    @contextmanager
    def stream(*args, **kwargs):
        yield SimpleNamespace(raise_for_status=lambda: None, iter_bytes=lambda _: iter([b'corrupt']))

    monkeypatch.setattr(installer.httpx, 'stream', stream)
    with pytest.raises(ValueError, match='checksum'):
        installer.install(tmp_path)
    assert not list(tmp_path.iterdir())
    destination = tmp_path / installer.FILENAME
    destination.write_bytes(b'previous local asset')
    with pytest.raises(ValueError, match='Existing projector'):
        installer.install(tmp_path)
    assert destination.read_bytes() == b'previous local asset'


def test_three_rejected_native_drawings_trigger_imajev_without_another_submit(qapp, monkeypatch):
    from app.core.contracts import ChoiceAnswer, Reply
    from app.inference.imajev_client import ImajevClient

    config = load_config(PROFILE)
    calls = []

    def native(endpoint, payload, timeout):
        calls.append('liquid')
        return native_reply(payload, probability=.7)

    def imajev(self, payload, image, timeout=None):
        calls.append('imajev')
        choices = {'symbol': 'X', 'cell': 'A1'}
        answers = {name: ChoiceAnswer(choice,
                   {key: float(key == choice) for key in question['criteria']}, 0., False)
                   for name, question in payload['questions'].items()
                   for choice in (choices[name],)}
        return Reply(config.recognition_fallback.model, answers,
                     {'model': config.recognition_fallback.model})

    class Opponent:
        manager = None
        def choose_move(self, payload, image):
            return MoveResult('place_B2', 'test-opponent', {})

    monkeypatch.setattr('app.inference.recognition.request_json', native)
    monkeypatch.setattr(ImajevClient, 'decide', imajev)
    controller = SessionController(TicTacToe(), create_recognizer(config), config, opponent=Opponent())
    controller.ready = True
    controller.new_game()
    controller.set_pending(INK)
    for attempt in (1, 2):
        controller.submit()
        wait_for(qapp, lambda: not controller.busy)
        assert controller.state.revision == 0
        assert controller.failed_recognition_retries == attempt
        assert controller.pending == INK
    controller.submit()
    wait_for(qapp, lambda: controller.state.revision == 2 and not controller.busy)
    assert calls == ['liquid', 'liquid', 'liquid', 'imajev']
    assert controller.failed_recognition_retries == 0
    assert controller.pending == ()
    fallback = [event for event in controller.events if event.get('fallback')]
    assert len(fallback) == 1
    assert fallback[0]['recognition']['backend'] == 'imajev'
    assert fallback[0]['recognition_threshold'] == .65
    assert fallback[0]['accepted_action'] == 'place_A1'


def test_restore_legacy_native_rejections_enables_fallback(qapp, tmp_path):
    config = load_config(PROFILE)
    controller = SessionController(TicTacToe(), create_recognizer(config), config)
    record = controller.record()
    record.pop('recognition_retries')
    record.pop('recognition_fallback')
    record['pending_ink'] = [{'points': list(stroke.points), 'width': stroke.width,
                              'color': stroke.color} for stroke in INK]
    record['events'] = [{'ticket': {'purpose': 'recognition', 'state_revision': 0},
                         'rejection': 'I could not identify the cell confidently.'}
                        for _ in range(3)]
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(record))
    controller.restore(path)
    assert controller.failed_recognition_retries == 3
    assert controller.pending == INK
    assert controller.record()['recognition_retries']['failed'] == 3
