from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import pytest
from app.config import Config, load_config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.storage.learning import LearningStore
from app.inference.service_manager import ServiceManager
from app.ui.window import Window
from scripts.serve_local import BusyGuard


def controller(qapp, tmp_path):
    return SessionController(TicTacToe(), object(), replace(Config(), learning_enabled=True, learning_directory=tmp_path))


def play(c, cells):
    for cell in cells:
        c.state = c.game.apply_action(c.state, 'place_' + cell)


def test_learning_prompt_has_no_assistance():
    game = TicTacToe()
    state = game.initial_state_for_player('O')
    for attempt in range(4):
        request = game.learning_request(state, game.legal_actions(state), 'retained lesson', attempt)
        assert request['state']['strategy'] == 'retained lesson'
        assert not set(request['state']) & {'opening_advice', 'immediate_O_win_actions', 'immediate_X_win_actions_if_unblocked'}
        assert 'Choose place_' not in request['questions']['move']['instructions']


@pytest.mark.parametrize('cells,expected', [(['A1', 'A2', 'B1', 'B2', 'C1'], True),
    (['A1', 'A2', 'B1', 'B2', 'C3', 'C2'], False),
    (['A1', 'B1', 'C1', 'B2', 'A2', 'C2', 'B3', 'A3', 'C3'], True)])
def test_terminal_triggers_and_resume(qapp, tmp_path, cells, expected):
    c = controller(qapp, tmp_path)
    calls = []
    c._coach = lambda: calls.append(c.session_id)
    play(c, cells)
    c._after_move()
    assert bool(calls) == expected
    if expected:
        request = c.learning_store.request(c.session_id)
        c.learning_store.commit(c.session_id, request, {'strategy': 'lesson'})
        path = tmp_path / 'sessions' / c.session_id / 'session.json'
        restored = controller(qapp, tmp_path)
        restored.restore(path)
        assert restored.session_id == c.session_id
        restored._coach = lambda: pytest.fail('duplicate coaching')
        restored._after_move()


def test_forced_draw(qapp, tmp_path):
    c = controller(qapp, tmp_path)
    c.state = c.game.initial_state_for_player('O')
    play(c, ['A1', 'B1', 'C1', 'B2', 'A2', 'C2', 'B3', 'A3'])
    calls = []
    c._coach = lambda: calls.append(True)
    c._launch('decision')
    assert calls and c.events[-1]['forced_action'] == 'place_C3'


def test_abandoned_and_toggle_snapshot(qapp, tmp_path):
    c = controller(qapp, tmp_path)
    c._coach = lambda: pytest.fail('abandoned game coached')
    c.next_learning = False
    assert c.learning
    c.new_game()
    assert not c.learning
    with pytest.raises(ValueError, match='not saved'):
        c.learning_store.request('missing')


def test_store_history_atomic_revisions_and_budget(qapp, tmp_path):
    c = controller(qapp, tmp_path)
    play(c, ['A1', 'A2', 'B1', 'B2', 'C1'])
    c._publish()
    request = c.learning_store.request(c.session_id)
    assert request['included_game_ids'] == [c.session_id]
    assert request['statistics'] == {'loss': 1}
    serialized = json.dumps(request)
    assert 'drawing' not in serialized and 'points' not in serialized and 'image' not in serialized
    c.learning_store.commit(c.session_id, request, {'strategy': 'lesson'})
    c.learning_store.commit(c.session_id, request, {'strategy': 'duplicate'})
    assert c.learning_store.ledger()['revision'] == 1
    assert c.learning_store.request(c.session_id)['previous_strategy'] == 'lesson'
    with pytest.raises(ValueError):
        c.learning_store.commit('another', request, {'strategy': 'bad', 'truncated': True})
    assert c.learning_store.ledger()['text'] == 'lesson'
    for i in range(30):
        other = dict(request['games'][0], game_id=str(i), events=[{'rejection': 'x' * 1000}])
        (tmp_path / 'games' / (str(i) + '.json')).write_text(json.dumps(other))
    bounded = c.learning_store.request(c.session_id)
    assert bounded['games'][0]['game_id'] == c.session_id
    assert len(json.dumps(bounded).encode()) < 9500


def test_window_retry_continue_gate(qapp, tmp_path):
    c = controller(qapp, tmp_path)
    c.start = lambda: None
    w = Window(c)
    c.phase = 'over'
    c.coaching_failed = True
    w.refresh()
    assert not w.new_button.isEnabled()
    assert w.retry_button.text() == 'Retry coaching'
    c.busy = True
    w.refresh()
    assert not w.continue_button.isEnabled()
    c.busy = False
    c.coaching_failed = False
    w.close()


def test_external_manager_and_owned_shutdown():
    manager = ServiceManager(replace(Config(), external_inference=True))
    manager.start()
    assert manager.process is None
    manager.shutdown()


def test_port_conflict(monkeypatch):
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connect_ex(self, address): return 0
    monkeypatch.setattr('app.inference.service_manager.socket.socket', Probe)
    with pytest.raises(RuntimeError, match='already occupied'):
        ServiceManager(Config()).start()


def test_config_learning(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('learning:\n  enabled: true\n  coach_backend: ollama\n')
    with pytest.raises(ValueError, match='requires learning.model'):
        load_config(path)


@pytest.mark.parametrize('path', ['/v1/systemone', '/v1/coach'])
def test_guard_both_endpoints(path):
    import asyncio
    state = SimpleNamespace(serving=False, lock=threading.Lock())
    state.lock.acquire()
    responses = []
    async def send(value): responses.append(value)
    async def downstream(*args): pytest.fail('busy request reached downstream')
    asyncio.run(BusyGuard(downstream, state)({'type': 'http', 'method': 'POST', 'path': path}, None, send))
    assert responses[0]['status'] == 409

@pytest.mark.parametrize('failure', [None, RuntimeError('generation failed'), KeyboardInterrupt()])
def test_generation_restores_adapter(monkeypatch, failure):
    import sys
    from contextlib import contextmanager, nullcontext
    from scripts.serve_local import generate_strategy
    trace = []
    class Tensor:
        shape = (1, 10)
        def to(self, device): return self
        def __getitem__(self, key): return self
        def tolist(self): return [1, 2]
    class Tokenizer:
        pad_token_id = 0
        eos_token_id = 2
        def apply_chat_template(self, *args, **kwargs):
            assert kwargs['enable_thinking'] is False
            return 'prompt'
        def __call__(self, *args, **kwargs): return {'input_ids': Tensor()}
        def decode(self, *args, **kwargs): return 'retained lesson'
    class Model:
        @contextmanager
        def disable_adapter(self):
            trace.append('disabled')
            try: yield
            finally: trace.append('restored')
        def generate(self, **kwargs):
            assert kwargs['do_sample'] is False and kwargs['max_new_tokens'] == 512
            if failure is not None: raise failure
            return Tensor()
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext,
        cuda=SimpleNamespace(is_available=lambda: False)))
    engine = SimpleNamespace(processor=SimpleNamespace(tokenizer=Tokenizer()), model=Model(), device='fake', readout=object())
    readout = engine.readout
    if failure is not None:
        with pytest.raises(type(failure)):
            generate_strategy(engine, {'games': [{'game_id': 'trigger'}]})
    else:
        assert generate_strategy(engine, {'games': [{'game_id': 'trigger'}]})['strategy'] == 'retained lesson'
    assert trace == ['disabled', 'restored'] and engine.readout is readout


def test_ollama_swap_sequence(monkeypatch):
    trace = []
    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload
    class Transport:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            trace.append(url.rsplit('/', 1)[-1])
            return Response({'models': [{'name': 'coach:latest'}]} if url.endswith('/tags') else {'models': []})
        def post(self, url, json):
            trace.append(url.rsplit('/', 1)[-1])
            assert json['keep_alive'] == 0
            return Response({'message': {'content': 'lesson'}, 'done_reason': 'stop'})
    monkeypatch.setattr('app.inference.service_manager.httpx.Client', Transport)
    manager = ServiceManager(replace(Config(), coach_backend='ollama', coach_model='coach'))
    manager.stop = lambda: trace.append('stop')
    def restart():
        assert not manager.swap_pending
        trace.append('reload')
    manager.start = restart
    assert manager.ollama({'games': []})['strategy'] == 'lesson'
    assert trace.index('stop') < trace.index('chat') < trace.index('generate') < trace.index('reload')
    assert trace[-2:] == ['ps', 'reload']


def test_unconfirmed_unload_blocks_restart():
    manager = ServiceManager(Config())
    manager.swap_pending = True
    with pytest.raises(RuntimeError, match='unload'):
        manager.start()


def test_strategy_injected_initial_and_retry(qapp, tmp_path, monkeypatch):
    from app.core.contracts import ChoiceAnswer, Reply
    from tests.test_session import wait_for
    class Client:
        def decide(self, request, image):
            requests.append(request)
            choice = next(iter(request['questions']['move']['criteria']))
            probabilities = {k: float(k == choice) for k in request['questions']['move']['criteria']}
            return Reply(Config.expected_model, {'move': ChoiceAnswer(choice, probabilities, 0, len(requests) == 1)}, {})
    requests = []
    coaching_requests = []
    def coach(client, request):
        coaching_requests.append(request)
        return {'strategy': 'coached lesson'}
    monkeypatch.setattr('app.core.session.shared_coach', coach)
    c = controller(qapp, tmp_path)
    c.client = Client()
    c.ready = True
    c.strategy = 'persistent lesson'
    c.state = c.game.initial_state_for_player('O')
    c._after_move()
    wait_for(qapp, lambda: not c.busy)
    assert c.phase == 'human'
    assert len(coaching_requests) == 1
    assert coaching_requests[0]['trigger']['type'] == 'decision_abstention'
    assert coaching_requests[0]['statistics'] == {}
    assert coaching_requests[0]['games'][0]['outcome'] == 'ongoing'
    assert len(requests) == 2
    assert requests[0]['state']['strategy'] == 'persistent lesson'
    assert requests[1]['state']['strategy'] == 'coached lesson'
    assert c.initial_strategy_revision == 0 and c.strategy_revision == 1
    assert c.strategy_updates[0]['state_revision'] == 0
    assert [e['strategy_revision'] for e in c.events if e.get('ticket', {}).get('purpose') == 'decision'] == [0, 1]
    assert requests[0]['questions']['move']['instructions'] != requests[1]['questions']['move']['instructions']
    assert not any(e.get('tactical_correction') for e in c.events)


def test_owned_process_shutdown(monkeypatch):
    calls = []
    class Process:
        pid = 1234
        running = True
        def poll(self): return None if self.running else 0
        def wait(self, timeout):
            calls.append(('wait', timeout))
            self.running = False
    monkeypatch.setattr('app.inference.service_manager.os.killpg', lambda pid, signal: calls.append(('terminate', pid)))
    manager = ServiceManager(Config())
    manager.process = Process()
    manager.shutdown()
    assert calls == [('terminate', 1234), ('wait', 30)]
    assert manager.process is None


def test_process_exit_readiness():
    manager = ServiceManager(Config())
    manager.process = SimpleNamespace(poll=lambda: 1)
    with pytest.raises(RuntimeError, match='child exited'):
        manager.check()


def test_learning_failure_retains_strategy(qapp, tmp_path):
    c = controller(qapp, tmp_path)
    play(c, ['A1', 'A2', 'B1', 'B2', 'C1'])
    c._publish()
    c.coach_request = c.learning_store.request(c.session_id)
    c._coach_completed(None, {'strategy': 'partial', 'truncated': True}, None, 1)
    assert c.coaching_failed and c.learning_store.ledger()['revision'] == 0
    old_id = c.session_id
    c.new_game()
    assert c.session_id == old_id


def test_revised_strategy_is_retained_next_game(qapp, tmp_path):
    c = controller(qapp, tmp_path)
    play(c, ['A1', 'A2', 'B1', 'B2', 'C1'])
    c._publish()
    first_id = c.session_id
    request = c.learning_store.request(first_id)
    c.learning_store.commit(first_id, request, {'strategy': 'first lesson'})
    c.new_game()
    assert c.strategy == 'first lesson' and c.strategy_revision == 1
    play(c, ['A1', 'A2', 'B1', 'B2', 'C1'])
    c._publish()
    request = c.learning_store.request(c.session_id)
    assert request['previous_strategy'] == 'first lesson'
    assert request['included_game_ids'] == [c.session_id, first_id]
    assert request['statistics'] == {'loss': 2}
    c.learning_store.commit(c.session_id, request, {'strategy': 'revised lesson'})
    assert c.strategy == 'first lesson'  # Finished game keeps its original snapshot.
    c.new_game()
    assert c.strategy == 'revised lesson' and c.strategy_revision == 2


def test_rejected_move_does_not_coach(qapp, tmp_path):
    from app.core.contracts import Reply, Ticket
    c = controller(qapp, tmp_path)
    c._coach = lambda: pytest.fail('rejected move coached')
    ticket = Ticket(c.session_id, 0, 'request', 'recognition')
    c.active = c.inflight = ticket
    c.busy = True
    c.phase = 'recognising'
    c.events.append({})
    c.game.decode_recognition = lambda *args: (_ for _ in ()).throw(ValueError('invalid ink'))
    c._completed(ticket, Reply(Config.expected_model, {}, {}), None, .1)
    assert c.phase == 'human' and c.state.revision == 0
    assert c.events[-1]['rejection'] == 'invalid ink'


class AbstainingClient:
    def __init__(self, always=True):
        self.requests = []
        self.always = always
    def decide(self, request, image):
        from app.core.contracts import ChoiceAnswer, Reply
        self.requests.append(request)
        choice = next(iter(request['questions']['move']['criteria']))
        probabilities = {k: float(k == choice) for k in request['questions']['move']['criteria']}
        abstained = self.always or len(self.requests) == 1
        return Reply(Config.expected_model, {'move': ChoiceAnswer(choice, probabilities, .8, abstained)},
                     {'answers': {'move': {'choice': choice, 'abstained': abstained}}})
    def warmup(self, request, image):
        from app.core.contracts import Reply
        return Reply(Config.expected_model, {}, {})


def test_repeated_abstention_coaches_then_pauses(qapp, tmp_path, monkeypatch):
    from test_session import wait_for
    calls = []
    def coach(client, request):
        calls.append(request)
        return {'strategy': 'lesson ' + str(len(calls))}
    monkeypatch.setattr('app.core.session.shared_coach', coach)
    c = controller(qapp, tmp_path)
    c.client = AbstainingClient()
    c.ready = True
    c.state = c.game.initial_state_for_player('O')
    c._after_move()
    wait_for(qapp, lambda: not c.busy)
    assert len(calls) == len(c.client.requests) == 2
    assert c.phase == 'error' and not c.coaching_failed
    assert c.state.revision == 0 and c.strategy_revision == 2
    assert 'Retry' in c.message
    # Two abstention updates don't mark terminal coaching as completed.
    assert not c.learning_store.updated(c.session_id)
    assert len(c.record()['learning']['updates']) == 2


def test_abstention_coaching_failure_retry_keeps_position(qapp, tmp_path, monkeypatch):
    from test_session import wait_for
    calls = []
    def coach(client, request):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError('coach unavailable')
        return {'strategy': 'recovery lesson'}
    monkeypatch.setattr('app.core.session.shared_coach', coach)
    c = controller(qapp, tmp_path)
    c.client = AbstainingClient(always=False)
    c.ready = True
    c.state = c.game.initial_state_for_player('O')
    c._after_move()
    wait_for(qapp, lambda: not c.busy)
    assert c.coaching_failed and c.state.revision == 0
    trigger_id = c.coach_trigger['update_id']
    c.retry()
    wait_for(qapp, lambda: not c.busy)
    assert not c.coaching_failed and c.state.revision == 1
    assert calls[0]['trigger']['update_id'] == calls[1]['trigger']['update_id'] == trigger_id
    assert c.strategy_revision == 1


def test_continue_after_abstention_coach_failure(qapp, tmp_path, monkeypatch):
    from test_session import wait_for
    monkeypatch.setattr('app.core.session.shared_coach', lambda *args: (_ for _ in ()).throw(RuntimeError('offline')))
    c = controller(qapp, tmp_path)
    c.client = AbstainingClient(always=False)
    c.ready = True
    c.state = c.game.initial_state_for_player('O')
    c._after_move()
    wait_for(qapp, lambda: not c.busy)
    c.continue_learning()
    wait_for(qapp, lambda: not c.busy)
    assert c.phase == 'human' and c.state.revision == 1
    assert c.strategy_revision == 0 and c.client.requests[-1]['state']['strategy'] == ''


def test_abstention_dedup_and_terminal_update_are_independent(qapp, tmp_path):
    c = controller(qapp, tmp_path)
    c._publish()
    trigger = {'type': 'decision_abstention', 'update_id': 'request-id'}
    request = c.learning_store.request(c.session_id, trigger=trigger)
    c.learning_store.commit(c.session_id, request, {'strategy': 'opening'})
    c.learning_store.commit(c.session_id, request, {'strategy': 'duplicate'})
    assert c.learning_store.ledger()['revision'] == 1
    assert not c.learning_store.updated(c.session_id)
    play(c, ['A1', 'A2', 'B1', 'B2', 'C1'])
    c._publish()
    request = c.learning_store.request(c.session_id)
    c.learning_store.commit(c.session_id, request, {'strategy': 'after loss'})
    assert c.learning_store.updated(c.session_id)
    assert c.learning_store.ledger()['revision'] == 2


def test_midgame_strategy_resume(qapp, tmp_path, monkeypatch):
    from test_session import wait_for
    monkeypatch.setattr('app.core.session.shared_coach', lambda *args: {'strategy': 'opening lesson'})
    c = controller(qapp, tmp_path)
    c.client = AbstainingClient(always=False)
    c.ready = True
    c.state = c.game.initial_state_for_player('O')
    c._after_move()
    wait_for(qapp, lambda: not c.busy)
    restored = controller(qapp, tmp_path)
    restored.restore(tmp_path / 'sessions' / c.session_id / 'session.json')
    assert restored.session_id == c.session_id
    assert restored.strategy == 'opening lesson' and restored.strategy_revision == 1
    assert restored.initial_strategy == '' and restored.initial_strategy_revision == 0
    assert restored.strategy_updates == c.strategy_updates
