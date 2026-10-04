import json

import pytest

from app.games.tic_tac_toe.game import CELLS, State, TicTacToe
from app.games.tic_tac_toe.prompting import (
    ARMS, COMPARE, COORDINATES, QUOTED_STRATEGY, candidate_facts,
    decision_request, fork_cells, normalize_choice, winning_cells,
)
from scripts.evaluate_prompting import (
    OwnedService, Trial, adoption_gate, bootstrap, prepare_dataset, reachable, score_answer,
)

GAME = TicTacToe()


def position(*cells, starter='X'):
    state = GAME.initial_state_for_player(starter)
    for cell in cells:
        state = GAME.apply_action(state, f'place_{cell}')
    return state


def test_quoted_context_and_every_output_fact_match_the_document():
    state = position('B2', 'C2', 'C3')
    request = decision_request(state, GAME.legal_actions(state), 'quoted')
    assert request['state'] == {
        'game': 'tic-tac-toe', 'player_to_move': 'O', 'opponent': 'X',
        'coordinates': [['A1', 'B1', 'C1'], ['A2', 'B2', 'C2'], ['A3', 'B3', 'C3']],
        'board': [['.', '.', '.'], ['.', 'X', 'O'], ['.', '.', 'X']],
        'strategy': QUOTED_STRATEGY,
    }
    question = request['questions']['move']
    assert question['instructions'] == COMPARE
    assert set(question['criteria']) == {'A1', 'B1', 'C1', 'A2', 'A3', 'B3'}
    for cell, facts in question['criteria'].items():
        assert facts == {'position': 'corner' if cell in ('A1', 'C1', 'A3') else 'side',
                         'wins_now': False, 'blocks_X_win_next_turn': cell == 'A1',
                         'allows_X_win_next_turn': cell != 'A1'}


@pytest.mark.parametrize('arm', ['context', 'A', 'B', 'C', 'expanded', 'text'])
def test_row_context_keeps_canonical_data_and_explicit_player_roles(arm):
    state = position('A1')
    request = decision_request(state, GAME.legal_actions(state), arm)
    context = request['state']
    assert context['you'] == 'O' and context['opponent'] == 'X'
    assert context['board'] == dict(zip(CELLS, state.board))
    assert context['coordinates'] == COORDINATES
    assert context['board_rows'] == [['X', '.', '.'], ['.', '.', '.'], ['.', '.', '.']]


def test_structured_and_text_control_flatten_identically():
    state = position('A1')
    structured = decision_request(state, GAME.legal_actions(state), 'expanded')
    text = decision_request(state, GAME.legal_actions(state), 'text')
    expected = {cell: '; '.join(f'{key}: {value if isinstance(value, str) else json.dumps(value)}'
                                for key, value in facts.items())
                for cell, facts in structured['questions']['move']['criteria'].items()}
    assert text['questions']['move']['criteria'] == expected
    assert structured['state'] == text['state']
    assert structured['questions']['move']['instructions'] == text['questions']['move']['instructions']


@pytest.mark.parametrize('arm', ARMS[1:])
def test_experimental_prompts_offer_every_legal_cell_without_recommended_actions(arm):
    state = position('B2', 'C2', 'C3')
    request = decision_request(state, GAME.legal_actions(state), arm)
    assert {normalize_choice(c) for c in request['questions']['move']['criteria']} == {
        a.id for a in GAME.legal_actions(state)}
    assert not {'opening_advice', 'immediate_O_win_actions', 'immediate_X_win_actions_if_unblocked',
                'best_move', 'minimax_score'} & request['state'].keys()
    assert 'Choose place_' not in request['questions']['move']['instructions']


def engine_wins(board, player):
    state = State(board=board, next_player=player)
    return tuple(CELLS.index(a.id[6:]) for a in GAME.legal_actions(state)
                 if GAME.outcome(GAME.apply_action(state, a.id)).winner == player)


def test_tactical_facts_against_rules_engine_for_both_starters():
    checked = 0
    for starter, count in [('X', 2097), ('O', 2423)]:
        states = list(reachable(starter))
        assert len(states) == count
        for state in states:
            before_threats = engine_wins(state.board, 'X')
            assert winning_cells(state.board, 'X') == before_threats
            for action in GAME.legal_actions(state):
                index = CELLS.index(action.id[6:])
                after = GAME.apply_action(state, action.id)
                facts = candidate_facts(state.board, index, expanded=True)
                threats = engine_wins(after.board, 'X')
                future_wins = engine_wins(after.board, 'O')
                assert facts['wins_now'] == (GAME.outcome(after).winner == 'O')
                assert facts['allows_X_win_next_turn'] == bool(threats)
                assert facts['blocks_X_win_next_turn'] == (bool(before_threats) and not threats)
                assert facts['number_of_next_turn_winning_moves'] == len(future_wins)
                assert facts['creates_fork'] == (len(future_wins) >= 2)
                # Independent rules-engine replay of every possible X fork reply.
                dangerous = []
                for reply in GAME.legal_actions(after):
                    after_x = GAME.apply_action(after, reply.id)
                    if GAME.outcome(after_x).kind == 'ongoing':
                        if len(engine_wins(after_x.board, 'X')) >= 2 and not engine_wins(after_x.board, 'O'):
                            dangerous.append(CELLS.index(reply.id[6:]))
                assert fork_cells(after.board, 'X', dangerous=True) == tuple(dangerous)
                assert facts['allows_opponent_fork'] == bool(dangerous)
                checked += 1
    assert checked > 15000


def test_fork_prevention_includes_forcing_defense_against_opposite_corners():
    state = position('A1', 'B2', 'C3')
    side = candidate_facts(state.board, CELLS.index('B1'), expanded=True)
    corner = candidate_facts(state.board, CELLS.index('C1'), expanded=True)
    assert side['blocks_opponent_fork'] and not side['allows_opponent_fork']
    assert corner['allows_opponent_fork'] and not corner['blocks_opponent_fork']


def test_scoring_counts_abstentions_as_failure_and_normalizes_cell_ids():
    state = position('B2', 'C2', 'C3')
    accepted = score_answer(state, {'choice': 'A1', 'abstained': False})
    abstained = score_answer(state, {'choice': 'A1', 'abstained': True})
    occupied = score_answer(state, {'choice': 'B2', 'abstained': False})
    assert accepted['action'] == 'place_A1' and accepted['accepted_optimal']
    assert abstained['ranked_optimal'] and not abstained['accepted_optimal'] and abstained['missed_block']
    assert not occupied['accepted'] and not occupied['legal']


def test_dataset_is_reproducible_disjoint_and_balances_starters(tmp_path):
    previous = tmp_path / 'previous.json'
    previous.write_text(json.dumps({'positions': [{'id': 'old', 'state': GAME.encode_state(position('A1'))}]}))
    dataset = prepare_dataset(previous, 123)
    assert dataset == prepare_dataset(previous, 123)
    diagnostic = {tuple(r['state']['board']) for r in dataset['diagnostic']}
    heldout = {tuple(r['state']['board']) for r in dataset['holdout']}
    assert len(dataset['diagnostic']) == 66 and len(heldout) == 256
    assert not diagnostic & heldout
    assert set(dataset['robustness_ids']) <= {r['id'] for r in dataset['holdout']}
    for phase in ('diagnostic', 'holdout'):
        assert {r['state']['starting_player'] for r in dataset[phase]} == {'X', 'O'}


def test_paired_bootstrap_uses_matching_boards_and_is_deterministic():
    def rows(arm, flags):
        return [{'label': f'holdout:{i}:canonical:{arm}', 'score': {'accepted_optimal': flag}}
                for i, flag in enumerate(flags)]
    candidate, baseline = rows('C', [True] * 20), rows('legacy', [False] * 20)
    assert bootstrap(candidate, baseline, 1) == bootstrap(candidate, baseline, 1)
    assert bootstrap(candidate, baseline, 1)['low'] == 1
    with pytest.raises(ValueError, match='identical'):
        bootstrap(candidate, baseline[:-1], 1)


def test_occupied_service_is_never_attached_or_stopped(monkeypatch, tmp_path):
    from app.config import Config
    import scripts.evaluate_prompting as harness

    class OccupiedSocket:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def connect_ex(self, address):
            return 0

    monkeypatch.setattr(harness.socket, 'socket', OccupiedSocket)
    monkeypatch.setattr(harness.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Must not launch a child'))
    with pytest.raises(RuntimeError, match='occupied'):
        with OwnedService(Config(), tmp_path):
            pytest.fail('Must not enter occupied service')
    assert not (tmp_path / 'service.log').exists()


def test_resume_reuses_recorded_call_and_rejects_changed_request(qapp, tmp_path):
    from app.config import Config
    from app.inference.imajev_client import parse_reply
    from test_protocol import answer

    class Client:
        calls = 0
        def decide(self, request, png, timeout):
            self.calls += 1
            return parse_reply(answer(request, {'move': 'A1'}), request, 'imajev-2b')

    (tmp_path / 'images').mkdir()
    client = Client()
    state = position('B2', 'C2', 'C3')
    trial = Trial(client, Config(), tmp_path)
    first = trial.call(state, 'quoted', 'diagnostic:example:canonical:quoted')
    resumed = Trial(client, Config(), tmp_path)
    assert resumed.call(state, 'quoted', first['label'])['answer'] == first['answer']
    assert client.calls == 1
    with pytest.raises(ValueError, match='mismatch'):
        resumed.call(state, 'quoted', first['label'], 'reverse')


def test_adoption_requires_all_safety_and_latency_gates():
    from types import SimpleNamespace

    calls = {}
    for phase in ('holdout', 'robustness'):
        for arm in ('legacy', 'raw', 'C'):
            for i in range(32):
                label = f'{phase}:{i}:canonical:{arm}'
                calls[label] = {'label': label, 'arm': arm, 'seconds': 2,
                                'answer': {'abstained': False}, 'score': {
                                    'accepted_optimal': arm == 'C' or i < 16,
                                    'ranked_optimal': arm == 'C' or i < 16,
                                    'legal': True, 'wins': [], 'required_block': False,
                                    'missed_win': False, 'missed_block': False,
                                    'forced_loss': False}}
    games = [{'arm': arm, 'outcome': 'draw', 'winner': None, 'aborted': None}
             for arm in ('legacy', 'raw', 'C')]
    trial = SimpleNamespace(calls=calls)
    assert adoption_gate(trial, 'C', games)['adopt']
    calls['holdout:0:canonical:C']['score']['missed_block'] = True
    assert not adoption_gate(trial, 'C', games)['adopt']
    assert not adoption_gate(trial, 'C', games)['checks']['no_block_regression']
    calls['holdout:0:canonical:C']['score']['missed_block'] = False
    for row in calls.values():
        if row['arm'] == 'C':
            row['seconds'] = 5
    assert not adoption_gate(trial, 'C', games)['checks']['latency_within_2x']


def test_default_dataset_input_is_tracked_and_reproduces_historical_sample():
    from scripts.evaluate_prompting import DEFAULT_PREVIOUS, SEED, digest, json_bytes

    previous = json.loads(DEFAULT_PREVIOUS.read_text())
    assert len(previous['positions']) == 21
    assert all(set(row) == {'id', 'state'} for row in previous['positions'])
    dataset = prepare_dataset(DEFAULT_PREVIOUS, SEED)
    assert len(dataset['diagnostic']) == 86 and len(dataset['holdout']) == 256
    assert GAME.decode_state(dataset['diagnostic'][0]['state']) == GAME.decode_state(previous['positions'][0]['state'])
    assert any(turn['drawing'] for row in previous['positions'] for turn in row['state']['history'])
    # Freeze every sampled board, stroke and option-order case from the completed trial.
    assert digest(json_bytes(dataset)) == '7baa51ddd6e408ca4e9262218ec3b0866193fb8de572d44b1eb6c29f82323ab2'
