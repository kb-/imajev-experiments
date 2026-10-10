"""Ordinary open fours and capture setups share the same bounded proof."""
from dataclasses import replace
import json
from pathlib import Path
import random

import pytest

from app.games.boku.game import Boku, State
from app.games.boku.geometry import CELLS
from app.games.boku.forced_wins import compact, forced_win_setup
from app.games.boku.tactics import black_winning_replies, completions


game = Boku()
FIXTURE = Path(__file__).resolve().parents[1] / 'docs/evaluation/fixtures/boku/open-four-session.json'


def before_open_four():
    record = json.loads(FIXTURE.read_text())
    return game.decode_state(record['events'][0]['state'])


def test_ordinary_setup_witness_survives_every_white_reply():
    state = game.apply_action(before_open_four(), 'place_D5')
    witness = forced_win_setup(game, compact(state))
    assert witness['Black_capture'] is None
    assert witness['Black_placement'] in ('place_C5', 'place_G8')
    after = game.apply_action(state, witness['Black_placement'])
    assert len(witness['threatened_winning_actions']) == 2
    for action in game.legal_actions(after):
        for _, end in completions(game, after, action.id):
            # Independently verify the recorded winning moves with the engine.
            assert any(game.outcome(win).winner == 'Black'
                       for winning in witness['threatened_winning_actions']
                       if winning in {a.id for a in game.legal_actions(end)}
                       for _, win in completions(game, end, winning))


@pytest.mark.parametrize('mode', ['quoted', 'coached_quoted'])
def test_defence_names_block_and_counter_threats_and_survives_retries(mode):
    state = before_open_four()
    actions = game.legal_actions(state)
    request = game.decision_request(state, actions, prompt_variant=mode)
    facts = request['questions']['move']['criteria']
    assert facts['place_D5']['allows_Black_forced_win']
    assert request['state']['immediate_White_win_actions'] == []
    assert request['state']['White_defensive_actions'] == ['place_C1', 'place_C5', 'place_E1']
    assert facts['place_G8']['allows_Black_forced_win']  # Separate capture trap.
    retry = game.retry_decision_request(state, actions, 1, prompt_variant=mode)
    assert retry['state'] == request['state']
    assert set(facts) == {a.id for a in actions}
    # Blocking and counter-attacking both refute the full checked setup search.
    for action in request['state']['White_defensive_actions']:
        assert forced_win_setup(game, compact(game.apply_action(state, action))) is None


def test_already_lost_position_does_not_invent_a_defensive_action():
    state = game.apply_action(before_open_four(), 'place_D5')
    state = game.apply_action(state, 'place_C5')
    request = game.decision_request(state, game.legal_actions(state), prompt_variant='quoted')
    assert request['state']['White_defensive_actions'] == []
    assert all(f['allows_Black_win_next_turn'] for a,f in request['questions']['move']['criteria'].items()
)


def test_reserve_draw_refutes_an_ordinary_setup():
    state = game.apply_action(before_open_four(), 'place_D5')
    state = compact(state, reserves=(state.reserves[0], 1))
    assert forced_win_setup(game, state) is None


def test_shortlisted_winning_replies_match_exhaustive_rules_engine():
    rng = random.Random(823)
    boards = [before_open_four().board]
    # Dense arbitrary nonterminal boards exercise all axes and blocked windows.
    boards += [tuple(rng.choice(('', '', 'Black', 'White')) for _ in CELLS) for _ in range(40)]
    for board in boards:
        for forbidden in (None, next((c for c, v in zip(CELLS, board) if not v), None)):
            state = State(board=board, next_player='Black', forbidden=forbidden)
            exhaustive = tuple(a.id for a in game.legal_actions(state)
                               if any(game.outcome(end).winner == 'Black'
                                      for _, end in completions(game, state, a.id)))
            assert black_winning_replies(state) == exhaustive


def test_original_and_non_threatened_positions_keep_their_existing_choices():
    state = game.apply_action(game.initial_state(), 'place_F10')
    quoted = game.decision_request(state, game.legal_actions(state), prompt_variant='quoted')
    assert quoted['state']['White_defensive_actions'] == []
    original = game.decision_request(state, game.legal_actions(state), prompt_variant='legacy')
    assert 'White_defensive_actions' not in original['state']
