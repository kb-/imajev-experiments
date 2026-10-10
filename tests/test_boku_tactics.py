from dataclasses import replace

import pytest

from app.games.boku.game import Boku, State
from app.games.boku.geometry import CELLS
from app.games.boku.tactics import black_winning_replies, candidate_facts


game = Boku()


def position(black=(), white=(), **kwargs):
    return State(board=tuple('Black' if c in black else 'White' if c in white else ''
                             for c in CELLS), next_player='White', **kwargs)


@pytest.mark.parametrize('mode', ['quoted', 'coached_quoted'])
def test_logged_loss_has_only_one_block_and_retries_retain_facts(mode):
    state = game.initial_state()
    for cell in ('E3','D2','E2','D1','E1','D3','D4','D5','E4'):
        state = game.apply_action(state,'place_'+cell)
    before = game.encode_state(state)
    actions = game.legal_actions(state)
    request = game.decision_request(state,actions,prompt_variant=mode)
    facts = request['questions']['move']['criteria']
    defaults = request['state']['candidate_fact_defaults']
    assert {a for a,f in facts.items() if f.get('blocks_Black_win_next_turn')} == {'place_E5'}
    assert {**defaults, **facts['place_E5']} == {
        'wins_now': False, 'blocks_Black_win_next_turn': True, 'allows_Black_win_next_turn': False,
        'allows_Black_capture_forced_win': False}
    assert facts['place_D6'] == {'allows_Black_win_next_turn': True}
    assert set(facts) == {a.id for a in actions}
    retry = game.retry_decision_request(state,actions,1,prompt_variant=mode)
    assert retry['questions']['move']['criteria'] == facts
    assert request['state']['immediate_White_win_actions'] == []
    assert retry['state']['immediate_White_win_actions'] == []
    assert game.encode_state(state) == before
    # Facts inform scoring; the app still accepts a legal, tactically bad choice.
    from app.core.contracts import ChoiceAnswer, Reply
    assert game.decode_decision(state,Reply('test',{'move':ChoiceAnswer('place_D6',{'place_D6':1},0,False)},{})) == 'place_D6'


def test_capture_choices_can_differ_in_whether_they_prevent_a_win():
    state = position(black=('E1','E2','E3','E4','F4'),white=('G4',))
    facts = candidate_facts(state, game.legal_actions(state))['place_D2']
    assert facts['blocks_Black_win_next_turn']
    assert not facts.get('allows_Black_win_next_turn',False)
    assert facts['capture_options'] == {
        'capture_E3': {'blocks_Black_win_next_turn': True},
        'capture_F4': {'allows_Black_win_next_turn': True}}
    capture = game.apply_action(state,'place_D2')
    request = game.decision_request(capture,game.legal_actions(capture),prompt_variant='quoted')
    assert request['questions']['move']['criteria'] == facts['capture_options']
    removed = game.apply_action(capture,'capture_E3')
    assert removed.forbidden == 'E3'
    assert not black_winning_replies(replace(removed,history=(),revision=0))


@pytest.mark.parametrize('mode', ['quoted', 'coached_quoted'])
def test_wins_are_checked_after_mandatory_capture_and_before_reserve_draw(mode):
    state = position(white=('F1','F2','F3','F4','I5'),black=('G5','H5'),reserves=(34,1))
    after = game.apply_action(state,'place_F5')
    assert after.phase == 'capture' and game.outcome(after).kind == 'ongoing'
    facts = candidate_facts(state,game.legal_actions(state))['place_F5']
    assert facts['wins_now'] and not facts.get('allows_Black_win_next_turn',False)
    assert facts['capture_options'] == {'capture_G5':{'wins_now':True},'capture_H5':{'wins_now':True}}
    assert all(f['wins_now'] for f in candidate_facts(after,game.legal_actions(after)).values())
    for probe, wins in ((state, ['place_F5']), (after, ['capture_G5', 'capture_H5'])):
        request = game.decision_request(probe,game.legal_actions(probe),prompt_variant=mode)
        retry = game.retry_decision_request(probe,game.legal_actions(probe),1,prompt_variant=mode)
        assert request['state']['immediate_White_win_actions'] == wins
        assert retry['state']['immediate_White_win_actions'] == wins
    opponent = position(black=('F1','F2','F3','F4','I5'),white=('G5','H5'),reserves=(1,34))
    opponent = replace(opponent,next_player='Black')
    assert 'place_F5' in black_winning_replies(opponent)


def test_forbidden_pocket_and_terminal_draw_do_not_invent_winning_replies():
    state = position(black=('E1','E2','E4','E5'), forbidden='E3')
    assert 'place_E3' not in black_winning_replies(replace(state,next_player='Black'))
    assert 'place_E3' in black_winning_replies(replace(state,next_player='Black',forbidden=None))
    draw = position(reserves=(36,1))
    assert all(not f for f in candidate_facts(draw,game.legal_actions(draw)).values())


def test_original_mode_retains_plain_descriptions():
    state = game.apply_action(game.initial_state(),'place_A1')
    request = game.decision_request(state,game.legal_actions(state),prompt_variant='legacy')
    assert all(isinstance(f,str) for f in request['questions']['move']['criteria'].values())
    assert 'candidate_fact_defaults' not in request['state']
    assert 'immediate_White_win_actions' not in request['state']
    assert 'immediate_White_win_actions' not in request['questions']['move']['instructions']


@pytest.mark.parametrize('mode', ['quoted', 'coached_quoted'])
def test_missed_A1_win_is_named_without_removing_other_choices(mode):
    state = position(white=('B2','C3','D4','E5'),black=('F6',))
    actions = game.legal_actions(state)
    for request in (game.decision_request(state,actions,prompt_variant=mode),
                    game.retry_decision_request(state,actions,1,prompt_variant=mode)):
        assert request['state']['immediate_White_win_actions'] == ['place_A1']
        assert set(request['questions']['move']['criteria']) == {a.id for a in actions}
        assert ('If immediate_White_win_actions is nonempty, choose one action from that list '
                'before considering any other action.') in request['questions']['move']['instructions']
