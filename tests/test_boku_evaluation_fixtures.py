"""Committed experiment inputs remain valid without local session recordings."""
import json
from pathlib import Path

import pytest

from app.games.boku.game import Boku
from app.games.boku.tactics import completions


FIXTURES = Path(__file__).resolve().parents[1] / 'docs/evaluation/fixtures/boku'


@pytest.mark.parametrize('name,revisions', [
    ('capture-trap-session.json', {25}),
    ('missed-win-session.json', {9, 25, 28, 29, 53}),
    ('winning-replay-session.json', {53}),
    ('open-four-session.json', {25, 27}),
    ('abstained-loss-session.json', {27, 29, 32}),
])
def test_committed_sessions_replay_and_supply_experiment_inputs(name, revisions):
    game = Boku()
    record = json.loads((FIXTURES / name).read_text())
    game.decode_state(record['state'])
    assert {e['ticket']['state_revision'] for e in record['events']} == revisions
    for event in record['events']:
        state = game.decode_state(event['state'])
        assert state.next_player == 'White'
        assert set(event['request']['questions']['move']['criteria']) == {
            a.id for a in game.legal_actions(state)}
        assert not event.get('image')
    if name == 'missed-win-session.json':
        event = next(e for e in record['events'] if e['ticket']['state_revision'] == 53)
        state = game.decode_state(event['state'])
        assert state.history[-1].action == 'place_F6'
        wins = {a.id for a in game.legal_actions(state)
                if any(game.outcome(end).winner == 'White'
                       for _, end in completions(game, state, a.id))}
        assert wins == {'place_A1'}
        assert event['accepted_action'] == 'place_B5'
    if name == 'winning-replay-session.json':
        assert game.outcome(game.decode_state(record['state'])).winner == 'White'
        assert record['events'][-1]['accepted_action'] == 'place_A1'


@pytest.mark.parametrize('path', sorted(FIXTURES.glob('boku-*.json')), ids=lambda p: p.name)
def test_archived_outputs_are_complete_and_have_portable_sources(path):
    record = json.loads(path.read_text())
    assert record['error'] is None
    for key in ('source', 'session'):
        if key in record:
            assert (FIXTURES / Path(record[key]).name).is_file()
    assert '/home/' not in path.read_text()
