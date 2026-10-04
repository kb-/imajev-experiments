from app.config import Config
from app.core.contracts import Stroke
from app.core.session import SessionController
from app.games.tic_tac_toe.game import State, TicTacToe
from app.inference.imajev_client import parse_reply
from test_protocol import answer
from test_session import Fake, wait_for


def sequence(*cells):
    game = TicTacToe()
    state = State()
    for cell in cells:
        state = game.apply_action(state, f'place_{cell}')
    return state


def test_unique_immediate_block_is_explicit_and_corrected():
    game = TicTacToe()
    state = sequence('B2', 'C2', 'C3')
    priorities, reason = game.tactical_priorities(state)
    assert priorities == ('place_A1',)
    assert reason == 'block immediate X win'
    request = game.decision_request(state, game.legal_actions(state))
    assert set(request['questions']['move']['criteria']) == {a.id for a in game.legal_actions(state)}
    assert 'Choose place_A1' in request['questions']['move']['instructions']
    assert request['state']['immediate_X_win_actions_if_unblocked'] == ['place_A1']
    assert game.tactical_choice(state, 'place_B3') == ('place_A1', reason)
    assert game.tactical_choice(state, 'place_A1') == ('place_A1', None)


def test_immediate_win_takes_priority_over_block():
    game = TicTacToe()
    state = sequence('A1', 'B1', 'A2', 'B2', 'C3')
    assert game.tactical_priorities(state) == (('place_B3',), 'winning move')
    assert game.tactical_choice(state, 'place_A3') == ('place_B3', 'winning move')
    assert game.outcome(game.apply_action(state, 'place_B3')).winner == 'O'


def test_no_forced_choice_without_a_unique_defense():
    game = TicTacToe()
    state = sequence('A1', 'A2', 'C1', 'C2', 'B2')
    assert game.tactical_priorities(state) == ((), None)
    assert game.tactical_choice(state, 'place_B1') == ('place_B1', None)
    assert game.tactical_choice(State(), 'place_B2') == ('place_B2', None)


class WrongMoveFake(Fake):
    def decide(self, request, image):
        self.calls.append(request)
        return parse_reply(answer(request, {'move': 'B3' if 'B3' in request['questions']['move']['criteria'] else 'place_B3'}), request, 'imajev-2b')


def test_controller_records_model_choice_and_tactical_correction(qapp):
    fake = WrongMoveFake()
    controller = SessionController(TicTacToe(), fake, Config())
    controller.ready = True
    controller.new_game()
    controller.state = sequence('B2', 'C2', 'C3')
    controller._launch('decision')
    wait_for(qapp, lambda: not controller.busy)
    assert controller.state.board[0] == 'O'
    assert controller.state.board[7] == ''
    event = controller.events[-1]
    assert event['model_proposed_action'] == 'place_B3'
    assert event['accepted_action'] == 'place_A1'
    assert event['tactical_correction']['reason'] == 'block immediate X win'
    assert 'tactical rule' in controller.last_move


def test_tactical_guard_can_be_disabled(qapp):
    from dataclasses import replace
    fake = WrongMoveFake()
    controller = SessionController(TicTacToe(), fake, replace(Config(), tactical_guard=False))
    controller.ready = True
    controller.new_game()
    controller.state = sequence('B2', 'C2', 'C3')
    controller._launch('decision')
    wait_for(qapp, lambda: not controller.busy)
    assert controller.state.board[7] == 'O'
    assert 'tactical_correction' not in controller.events[-1]


def test_tactical_guard_across_reachable_o_turns():
    from scripts.evaluate import o_turn_states
    game = TicTacToe()
    checked = guarded = 0
    for state in o_turn_states():
        legal = game.legal_actions(state)
        priorities, reason = game.tactical_priorities(state)
        for action in legal:
            committed, correction = game.tactical_choice(state, action.id)
            assert committed in {a.id for a in legal}
            if priorities:
                assert committed in priorities
                assert (correction is not None) == (action.id not in priorities)
                guarded += 1
            else:
                assert committed == action.id and correction is None
            checked += 1
    assert checked == 7536
    assert guarded > 0
