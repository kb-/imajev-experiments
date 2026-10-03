import threading
import time
from dataclasses import replace
from PyQt6.QtCore import QCoreApplication
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe, State
from app.inference.imajev_client import parse_reply
from test_protocol import INK, answer


def wait_for(qapp, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        qapp.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for worker')
        time.sleep(.005)
    qapp.processEvents()


class Fake:
    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.calls = []
        self.error = None
    def warmup(self, request, image):
        return self.decide(request, image)
    def decide(self, request, image):
        self.calls.append(request)
        self.gate.wait(3)
        if self.error:
            raise RuntimeError(self.error)
        choices = {'symbol': 'X', 'cell': 'A1'} if 'symbol' in request['questions'] else {'move': next(iter(request['questions']['move']['criteria']))}
        return parse_reply(answer(request, choices), request, 'imajev-2b')


def make(qapp):
    fake = Fake()
    c = SessionController(TicTacToe(), fake, Config())
    c.ready = True
    c.new_game()
    return c, fake


def test_double_submit_and_reset_during_inference(qapp):
    c, fake = make(qapp)
    fake.gate.clear()
    c.set_pending(INK)
    c.submit()
    c.submit()
    wait_for(qapp, lambda: len(fake.calls) == 1)
    c.new_game()
    assert c.phase == 'loading'
    assert c.state.revision == 0
    fake.gate.set()
    wait_for(qapp, lambda: not c.busy)
    assert c.phase == 'human' and c.state.revision == 0 and not c.pending
    assert len(fake.calls) == 1


def test_error_retains_ink_retry_does_not_duplicate(qapp):
    c, fake = make(qapp)
    fake.error = 'request timed out'
    c.set_pending(INK)
    c.submit()
    old = c.active
    wait_for(qapp, lambda: not c.busy)
    assert c.phase == 'error' and c.pending == INK and c.state.revision == 0
    fake.error = None
    fake.gate.clear()
    c.retry()
    current = c.active
    c.retry()
    c._completed(old, None, None, 1)  # A late duplicate must not release the new worker gate.
    assert c.busy and c.active == current
    fake.gate.set()
    wait_for(qapp, lambda: c.phase == 'human' and not c.busy)
    assert c.state.revision == 2 and not c.pending


def test_computer_failure_preserves_human_move(qapp):
    c, fake = make(qapp)
    original = fake.decide
    def decide(req, image):
        if 'move' in req['questions']:
            raise RuntimeError('GPU out of memory')
        return original(req, image)
    fake.decide = decide
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: c.phase == 'error')
    assert c.state.revision == 1 and c.state.next_player == 'O'
    fake.decide = original
    c.retry()
    wait_for(qapp, lambda: c.phase == 'human')
    assert c.state.revision == 2


class TinyGame:
    """Test-only second game: two picks total, no board cells or X/O assumptions."""
    id = 'tiny'
    name = 'Tiny'
    human_player = 'person'
    computer_player = 'model'
    instruction = 'Make a mark.'
    def initial_state(self): return 0
    def revision(self, state): return state
    def current_player(self, state): return 'person' if state == 0 else 'model'
    def legal_actions(self, state):
        from app.core.contracts import Action
        return (Action('take', 'Take token'),) if state < 2 else ()
    def outcome(self, state):
        from app.core.contracts import Outcome
        return Outcome('draw' if state == 2 else 'ongoing')
    def apply_action(self, state, action, drawing=()):
        assert action == 'take' and state < 2
        return state + 1
    def render(self, state, drawing, purpose):
        from app.core.contracts import Scene
        return Scene(strokes=drawing)
    def recognition_request(self, state, drawing):
        return {'state': {}, 'questions': {'symbol': {'type': 'choice', 'criteria': {'X': None, 'O': None}}, 'cell': {'type': 'choice', 'criteria': {'A1': None, 'B1': None}}}}
    def decode_recognition(self, state, drawing, reply, threshold): return 'take'
    def encode_state(self, state): return {'version': 1, 'game': self.id, 'count': state}
    def decode_state(self, record): return record['count']


def test_generic_controller_and_forced_action(qapp):
    fake = Fake()
    c = SessionController(TinyGame(), fake, Config())
    c.ready = True
    c.new_game()
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: c.phase == 'over')
    assert c.state == 2 and len(fake.calls) == 1
    assert c.events[-1]['forced_action'] == 'take'
