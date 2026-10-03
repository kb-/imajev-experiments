import json
import pytest
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from test_session import make, Fake, wait_for


def test_new_games_alternate_starters_and_begin_computer_turn(qapp):
    controller, fake = make(qapp)
    assert controller.state.starting_player == 'X'
    assert controller.editable
    controller.new_game(alternate_starter=True)
    assert controller.state.starting_player == 'O'
    assert not controller.editable
    wait_for(qapp, lambda: controller.phase == 'human')
    assert controller.state.revision == 1
    assert controller.state.board.count('O') == 1
    assert controller.events[-1]['request']['state']['starting_player'] == 'O'
    controller.new_game(alternate_starter=True)
    assert controller.state.starting_player == 'X'
    assert controller.state.revision == 0 and controller.editable
    assert len(fake.calls) == 1


def test_reset_during_inference_waits_then_starts_correct_player(qapp):
    controller, fake = make(qapp)
    fake.gate.clear()
    controller.new_game(alternate_starter=True)
    stale = controller.active
    wait_for(qapp, lambda: len(fake.calls) == 1)
    controller.new_game(alternate_starter=True)  # X
    controller.new_game(alternate_starter=True)  # O
    assert controller.phase == 'loading' and controller.state.starting_player == 'O'
    assert len(fake.calls) == 1
    fake.gate.set()
    wait_for(qapp, lambda: controller.phase == 'human')
    assert controller.state.revision == 1 and controller.state.board.count('O') == 1
    assert len(fake.calls) == 2
    assert all(e['ticket']['request_id'] != stale.request_id for e in controller.events)


def test_o_started_game_roundtrips_and_resumes(qapp, tmp_path):
    game = TicTacToe()
    controller = SessionController(game, Fake(), Config())
    controller.state = game.initial_state_for_player('O')
    for action in ('place_A1', 'place_B2', 'place_C1'):
        controller.state = game.apply_action(controller.state, action)
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(controller.record()))
    resumed = SessionController(game, Fake(), Config())
    resumed.restore(path)
    assert resumed.state == controller.state
    assert game.decode_state(game.encode_state(resumed.state)) == resumed.state
    resumed.start()
    wait_for(qapp, lambda: resumed.editable)
    assert resumed.state.revision == 3
    resumed.new_game(alternate_starter=True)
    assert resumed.state.starting_player == 'X' and resumed.editable
    old = game.encode_state(game.initial_state())
    del old['starting_player']
    assert game.decode_state(old) == game.initial_state()


def test_first_o_request_and_retry_respect_disabled_opening_suggestion():
    game = TicTacToe()
    state = game.initial_state_for_player('O')
    actions = game.legal_actions(state)
    for request in (game.decision_request(state, actions, opening_suggestion=False),
                    game.retry_decision_request(state, actions, 1, opening_suggestion=False)):
        assert request['state']['opening_advice'] is None
        assert request['state']['starting_player'] == 'O'
        assert 'place_B2' not in request['questions']['move']['instructions']
    assert game.decision_request(state, actions)['state']['opening_advice'] == 'place_B2'
    with pytest.raises(ValueError):
        game.initial_state_for_player('invalid')
