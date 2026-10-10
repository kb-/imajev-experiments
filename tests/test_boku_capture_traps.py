"""Regression proof for the experimental capture-trap fact, not app integration."""
from dataclasses import replace

import pytest

from app.games.boku.game import Boku
from app.games.boku.tactics import completions
from app.games.boku.capture_traps import evaluate, capture_trap


game = Boku()


def before_trap():
    state = game.initial_state()
    for action in ('place_E3','place_D2','place_E2','place_D1','place_E1','place_D3',
                   'place_D4','place_D5','place_E4','place_E5','place_C5','place_D6',
                   'place_F5','capture_E5','place_G5','capture_E4','place_D7',
                   'capture_D5','place_E4','place_C2','capture_E4','place_C3',
                   'place_E4','place_E5','place_B1'):
        state = game.apply_action(state, action)
    return state


def test_trap_witness_survives_every_complete_white_reply():
    state = before_trap()
    result = evaluate(game,state,'place_D5')
    assert result['allows_Black_capture_forced_win']
    witness = result['branches'][0]['witness']
    assert witness['Black_placement'] == 'place_B3'
    assert witness['Black_capture'] == 'capture_D3'
    assert witness['White_reply_actions_checked'] == 61
    state = game.apply_action(state,'place_D5')
    state = game.apply_action(state,witness['Black_placement'])
    state = game.apply_action(state,witness['Black_capture'])
    assert state.forbidden == 'D3'
    assert 'place_D3' not in {a.id for a in game.legal_actions(state)}
    for action in game.legal_actions(state):
        for _, after in completions(game,state,action.id):
            assert game.outcome(after).kind == 'ongoing'
            for _, end in completions(game,after,'place_D3'):
                assert game.outcome(end).winner == 'Black'


@pytest.mark.parametrize('action', ['place_B3','place_C4','place_F4'])
def test_prevention_and_counter_capture_are_not_falsely_flagged(action):
    state = before_trap()
    assert not evaluate(game,state,action)['allows_Black_capture_forced_win']
    if action == 'place_B3':
        return
    for move in (action,'place_B3','capture_D3',
                 'place_F4' if action=='place_C4' else 'place_C4','capture_E4'):
        state = game.apply_action(state,move)
    assert game.outcome(state).kind == 'ongoing'
    for move in game.legal_actions(state):
        assert all(game.outcome(end).winner != 'Black'
                   for _, end in completions(game,state,move.id))


def test_white_winning_reply_refutes_a_capture_trap():
    state = game.apply_action(before_trap(),'place_D5')
    board = list(state.board)
    from app.games.boku.geometry import CELLS
    for c in ('A1','A2','A3','A4'):
        board[CELLS.index(c)] = 'White'
    state = replace(state,board=tuple(board))
    assert capture_trap(game,state) is None


def test_app_prompt_contains_the_capture_trap_fact():
    state = before_trap()
    request = game.decision_request(state,game.legal_actions(state),prompt_variant='quoted')
    facts = request['questions']['move']['criteria']
    assert request['state']['candidate_fact_defaults']['allows_Black_capture_forced_win'] is False
    assert facts['place_D5']['allows_Black_capture_forced_win']
    assert not any(facts[a].get('allows_Black_capture_forced_win')
                   for a in ('place_B3','place_C4','place_F4'))


def test_decision_preparation_is_background_and_reset_skips_inference(qapp,tmp_path,monkeypatch):
    import threading
    from app.games.boku.policy import Policy
    from test_game_separation import make_controller
    from test_session import wait_for
    c = make_controller(tmp_path,'quoted')
    c.state = game.apply_action(c.state,'place_E3')
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    threads = []
    original = Policy.decision
    def delayed(self,*args,**kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Policy,'decision',delayed)
    try:
        c._launch('decision')
        wait_for(qapp,entered.is_set)
        assert c.busy and c.message == 'Checking move consequences…'
        assert c.events[-1]['request'] is None
        assert threads == [threads[0]] and threads[0] != main_thread
        c.new_game()
    finally:
        release.set()
    wait_for(qapp,lambda:not c.busy)
    assert c.state.revision == 0 and not c.client.calls


def test_prepared_request_is_recorded_before_answer_is_applied(qapp,tmp_path):
    from test_game_separation import make_controller
    from test_session import wait_for
    c = make_controller(tmp_path,'quoted')
    c.state = game.apply_action(c.state,'place_E3')
    c._launch('decision')
    wait_for(qapp,lambda:not c.busy)
    assert c.events[-1]['request'] == c.client.calls[-1]
    assert c.events[-1]['request']['state']['candidate_fact_defaults']['allows_Black_capture_forced_win'] is False
    assert c.state.revision == 2
