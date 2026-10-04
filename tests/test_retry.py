from pathlib import Path
import json
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import State, TicTacToe, CELLS
from app.inference.imajev_client import parse_reply
from scripts.evaluate import oracle
from app.ui.window import format_question_history
from test_protocol import answer
from test_session import wait_for


class AbstainOnce:
    def __init__(self):
        self.requests = []
    def warmup(self, request, image):
        return parse_reply(answer(request, {'symbol': 'invalid', 'cell': 'invalid'}, abstained=True), request, 'imajev-2b')
    def decide(self, request, image):
        self.requests.append(request)
        return parse_reply(answer(request, {'move': 'place_B2'}, abstained=len(self.requests) == 1), request, 'imajev-2b')


def test_retry_changes_request_and_preserves_accepted_x(qapp):
    game, fake = TicTacToe(), AbstainOnce()
    c = SessionController(game, fake, Config(prompt_variant='legacy'))
    c.ready = True
    c.new_game()
    c.state = game.apply_action(State(), 'place_A1')
    c._launch('decision')
    wait_for(qapp, lambda: c.phase == 'error')
    assert c.state.revision == 1
    assert 'abstained' in c.message
    c.retry()
    wait_for(qapp, lambda: c.phase == 'human')
    assert c.state.board[0] == 'X' and c.state.board[4] == 'O'
    assert c.state.revision == 2
    assert fake.requests[0] != fake.requests[1]
    assert c.events[-1]['decision_attempt'] == 1
    history = format_question_history(c.events)
    assert 'Decision · retry 1' in history
    assert fake.requests[0]['questions']['move']['instructions'] in history
    assert fake.requests[1]['questions']['move']['instructions'] in history
    assert 'Result: Imajev abstained' in history


def test_resume_paused_computer_turn(qapp, tmp_path):
    game, fake = TicTacToe(), AbstainOnce()
    original = SessionController(game, fake, Config(prompt_variant='legacy'))
    original.ready = True
    original.new_game()
    original.state = game.apply_action(State(), 'place_A1')
    original._launch('decision')
    wait_for(qapp, lambda: original.phase == 'error')
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(original.record()))
    resumed_fake = AbstainOnce()
    resumed_fake.requests.append({'earlier': 'abstention'})
    resumed = SessionController(game, resumed_fake, Config(prompt_variant='legacy'))
    resumed.restore(path)
    assert resumed.session_id != original.session_id
    assert resumed.source_session_id == original.session_id
    assert resumed.decision_attempt == 1
    resumed.start()
    wait_for(qapp, lambda: resumed.phase == 'human')
    assert resumed.state.revision == 2
    assert resumed.state.board[4] == 'O'
    assert resumed.events[-1]['decision_attempt'] == 1


def test_opening_guidance_is_safe_for_every_first_x():
    game = TicTacToe()
    for cell in CELLS:
        state = game.apply_action(State(), f'place_{cell}')
        request = game.decision_request(state, game.legal_actions(state))
        suggested = request['state']['opening_advice']
        assert suggested in request['questions']['move']['criteria']
        assert f'Choose {suggested}' in request['questions']['move']['instructions']
        after = game.apply_action(state, suggested)
        assert oracle(after.board, after.next_player) == 0


def test_retries_change_question_after_repeated_abstention():
    game = TicTacToe()
    state = State()
    for action in ('place_B2', 'place_C1', 'place_A3'):
        state = game.apply_action(state, action)
    actions = game.legal_actions(state)
    first = game.retry_decision_request(state, actions, 1)
    second = game.retry_decision_request(state, actions, 2)
    assert first != second
    assert 'place_A1' in first['questions']['move']['instructions']
    assert 'place_B1' in second['questions']['move']['instructions']


def test_opening_suggestion_can_be_disabled_without_disabling_tactics():
    game = TicTacToe()
    state = game.apply_action(State(), 'place_A1')
    actions = game.legal_actions(state)
    first = game.decision_request(state, actions, opening_suggestion=False)
    retry = game.retry_decision_request(state, actions, 1, opening_suggestion=False)
    assert first['state']['opening_advice'] is None
    assert retry['state']['opening_advice'] is None
    assert 'place_B2' not in first['questions']['move']['instructions']
    assert 'place_B2' not in retry['questions']['move']['instructions']
    assert first['state']['rules'] == retry['state']['rules']
