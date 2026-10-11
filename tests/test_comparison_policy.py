"""Comparison trials use the gameplay policy and freeze shared learned rules."""
from dataclasses import replace
import json
import random
import sys
from types import SimpleNamespace

import pytest

from app.config import Config
from app.core.contracts import MoveResult
from app.core.move_sampling import select_move
from app.core.registry import get_game
from app.core.session import SessionController
from app.storage.atomic import atomic_json
from app.storage.coached import CoachedStore
from app.storage.rules import save_rules
import scripts.compare_models as comparison
from test_boku_forced_wins import before_open_four
from test_session import Fake, wait_for


def boku_position(kind):
    game = get_game('boku')
    if kind in ('defence', 'lost'):
        state = before_open_four()
        if kind == 'lost':
            for action in ('place_D5', 'place_C5'):
                state = game.apply_action(state, action)
        return state
    cells = (('K1', 'A1', 'K3', 'A2', 'K5', 'A3', 'J1', 'A4', 'J6') if kind == 'win' else
             ('E6', 'D6', 'E7', 'A1', 'E8', 'A3', 'E9', 'B1', 'F8', 'G8'))
    state = game.initial_state()
    for cell in cells:
        state = game.apply_action(state, 'place_' + cell)
    assert state.next_player == 'White'
    return state


def case_for(game, state, config):
    request, tactical = game.session_policy.prepare_decision(
        game, state, game.legal_actions(state), config, game.session_policy.coaching.initial_strategy, 0)
    return {'id': '0000:canonical', 'state': game.encode_state(state), 'request': request,
            'request_sha256': comparison.digest(comparison.encoded(request)), 'image_sha256': 'image',
            'order': 'canonical', 'evidence': comparison.evidence(game, state), 'tactical_evidence': tactical}


@pytest.mark.parametrize('kind,proposal,expected', [
    ('win', 'place_B1', 'place_A5'), ('defence', 'place_D5', 'place_C5'),
    ('capture', 'capture_F8', 'capture_E7'), ('lost', 'place_A1', 'place_A1')])
@pytest.mark.parametrize('mode,guard', [('quoted', True), ('legacy', True), ('quoted', False), ('coached_quoted', True)])
def test_boku_comparison_matches_gameplay_policy(monkeypatch, kind, proposal, expected, mode, guard):
    monkeypatch.setattr(comparison, 'memory', lambda: {'gpu_device_mib': None})
    game = get_game('boku')
    state = boku_position(kind)
    config = Config(game='boku', prompt_variant=mode, tactical_guard=guard)
    case = case_for(game, state, config)
    reply = MoveResult(proposal, 'test', {'original': proposal}, 'ollama',
                       probabilities={proposal: .9, 'place_C5': .8, 'place_C1': .2, 'place_E1': .1})
    result = comparison.run_case(game, config, SimpleNamespace(choose_move=lambda *_: reply), case, b'', 42)
    assert result['status'] == 'valid', result
    action, reason = game.session_policy.correct_action(game, state, proposal, config,
                                                        case['tactical_evidence'], reply.probabilities)
    assert action == (expected if guard and mode != 'coached_quoted' else proposal)
    assert result['raw_proposal'] == result['sampled_action'] == proposal
    assert result['accepted_action'] == result['selected'] == action
    assert result['tactical_correction'] == reason
    assert result['raw_score'] == comparison.score(proposal, case['evidence'])
    assert result['assisted_score'] == comparison.score(action, case['evidence'])
    if mode == 'legacy':
        assert case['request'] == game.decision_request(state, game.legal_actions(state), prompt_variant=mode)


def test_sampling_precedes_guard_and_records_original_proposal(monkeypatch):
    monkeypatch.setattr(comparison, 'memory', lambda: {'gpu_device_mib': None})
    game, state = get_game('boku'), before_open_four()
    config = Config(game='boku', move_temperature=.5)
    case = case_for(game, state, config)
    reply = MoveResult('place_D5', 'test', {}, probabilities={'place_D5': .1, 'place_C5': .9})
    proposed, sampled, distribution = select_move(game, state, reply, .5, random.Random('42:0000:canonical'))
    result = comparison.run_case(game, config, SimpleNamespace(choose_move=lambda *_: reply), case, b'', 42)
    assert proposed == result['proposed_action'] == 'place_D5'
    assert sampled == result['sampled_action'] == 'place_C5'
    assert result['sampled_distribution'] == distribution
    assert result['selected'] == 'place_C5' and result['tactical_correction'] is None


def test_comparison_and_controller_record_same_guard_result(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, 'memory', lambda: {'gpu_device_mib': None})
    game, state = get_game('boku'), before_open_four()
    config = Config(game='boku', directory=tmp_path)
    reply = MoveResult('place_D5', 'test', {}, probabilities={'place_C5': .8, 'place_C1': .2})
    opponent = SimpleNamespace(choose_move=lambda *_: reply)
    c = SessionController(game, Fake(), config, opponent=opponent)
    c.ready = True
    c.state = state
    c._launch('decision')
    wait_for(qapp, lambda: not c.busy, timeout=10)
    event = c.events[-1]
    row = comparison.run_case(game, config, opponent, case_for(game, state, config), b'', 42)
    assert event['model_proposed_action'] == row['proposed_action']
    assert event['accepted_action'] == row['accepted_action'] == 'place_C5'
    assert event['tactical_correction']['reason'] == row['tactical_correction']


@pytest.mark.parametrize('mode', ['quoted', 'legacy', 'coached_quoted'])
def test_tic_tac_toe_guard_behavior_is_preserved(monkeypatch, mode):
    monkeypatch.setattr(comparison, 'memory', lambda: {'gpu_device_mib': None})
    game = get_game('tic_tac_toe')
    state = game.initial_state()
    for cell in ('A1', 'B1', 'A2', 'B2', 'C3'):
        state = game.apply_action(state, 'place_' + cell)
    config = Config(prompt_variant=mode)
    case = case_for(game, state, config)
    choice = 'A3' if mode != 'legacy' else 'place_A3'
    reply = MoveResult(choice, 'test', {})
    row = comparison.run_case(game, config, SimpleNamespace(choose_move=lambda *_: reply), case, b'', 42)
    assert row['status'] == 'valid', row
    assert row['selected'] == ('place_A3' if mode == 'coached_quoted' else 'place_B3')


def profile(tmp_path, game_id, name, rules):
    root = tmp_path / name
    game = get_game(game_id)
    policy = game.session_policy.coaching
    store = CoachedStore(policy.storage_root(root), policy)
    ledger = store.ledger()
    ledger.update(strategy=rules, revision=3)
    atomic_json(store.root / 'strategy.json', ledger)
    path = tmp_path / (name + '.yaml')
    path.write_text(f'game:\n  default: {game_id}\nopponent:\n  prompt_variant: coached_quoted\nlearning:\n  directory: {root}\n')
    return path, store.root / 'strategy.json'


@pytest.mark.parametrize('game_id', ['tic_tac_toe', 'boku'])
def test_common_learned_rules_and_freeze_resume(qapp, tmp_path, monkeypatch, game_id):
    game = get_game(game_id)
    state = (game.apply_action(game.initial_state(), 'place_A1') if game_id == 'tic_tac_toe' else before_open_four())
    monkeypatch.setattr(comparison, 'states', lambda _: iter([state]))
    rules = ['first priority', 'second priority']
    first, ledger1 = profile(tmp_path, game_id, 'first', rules)
    second, ledger2 = profile(tmp_path, game_id, 'second', rules)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    output = tmp_path / 'experiment'
    argv = ['compare', '--game', game_id, '--config', str(first), '--config', str(second),
            '--output', str(output), '--limit', '1', '--prepare-only']
    monkeypatch.setattr(sys, 'argv', argv)
    comparison.main()
    protocol = json.loads((output / 'protocol.json').read_text())
    assert protocol['strategy'] == rules and protocol['version'] == comparison.VERSION
    cases = json.loads((output / 'cases.json').read_text())
    assert all(c['request']['state']['strategy'] == rules for c in cases)
    canonical = list(cases[0]['request']['questions']['move']['criteria'])
    assert list(cases[1]['request']['questions']['move']['criteria']) == canonical[::-1]
    assert set(cases[2]['request']['questions']['move']['criteria']) == set(canonical)
    assert all(p.read_bytes() == value for p, value in before.items())
    assert set(tmp_path.rglob('strategy.json')) == set(before)
    ledger1.unlink()
    ledger2.write_text('malformed live ledger')
    export = tmp_path / 'export.json'
    monkeypatch.setattr(sys, 'argv', argv + ['--resume', '--save-rules', str(export)])
    comparison.main()
    assert json.loads(export.read_text())['strategy'] == rules
    assert not ledger1.exists() and ledger2.read_text() == 'malformed live ledger'


@pytest.mark.parametrize('source', ['file', 'basic'])
def test_different_profile_rules_require_explicit_shared_source(qapp, tmp_path, monkeypatch, source):
    first, _ = profile(tmp_path, 'tic_tac_toe', 'first', ['first model'])
    second, _ = profile(tmp_path, 'tic_tac_toe', 'second', ['second model'])
    output = tmp_path / 'experiment'
    argv = ['compare', '--config', str(first), '--config', str(second), '--output', str(output), '--limit', '1', '--prepare-only']
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit):
        comparison.main()
    rules_file = tmp_path / 'rules.json'
    save_rules(rules_file, 'tic_tac_toe', ['shared', 'ordered'])
    flags = ['--rules-file', str(rules_file)] if source == 'file' else ['--basic-rules']
    monkeypatch.setattr(sys, 'argv', argv + flags)
    comparison.main()
    expected = ['shared', 'ordered'] if source == 'file' else get_game('tic_tac_toe').session_policy.coaching.initial_strategy
    assert json.loads((output / 'protocol.json').read_text())['strategy'] == expected
    rules_file.unlink()
    monkeypatch.setattr(sys, 'argv', argv + ['--resume'])
    comparison.main()
    monkeypatch.setattr(sys, 'argv', argv + ['--resume'] + flags)
    with pytest.raises(SystemExit):
        comparison.main()


def test_old_comparison_protocol_is_rejected(qapp, tmp_path, monkeypatch):
    config, _ = profile(tmp_path, 'tic_tac_toe', 'first', ['first'])
    output = tmp_path / 'experiment'
    output.mkdir()
    atomic_json(output / 'protocol.json', {'version': 'opponent-comparison-v1'})
    monkeypatch.setattr(sys, 'argv', ['compare', '--config', str(config), '--output', str(output), '--resume', '--prepare-only'])
    with pytest.raises(SystemExit):
        comparison.main()


def test_preparation_freezes_guard_evidence_without_option_order_bias(qapp, tmp_path, monkeypatch):
    game, state = get_game('boku'), before_open_four()
    monkeypatch.setattr(comparison, 'states', lambda _: iter([state]))
    (tmp_path / 'images').mkdir()
    config = Config(game='boku', prompt_variant='legacy')
    cases = comparison.prepare([config], game, 1, 42, tmp_path, [])
    for case in cases:
        assert set(case['tactical_evidence']['defensive_actions']) == {'place_C1', 'place_C5', 'place_E1'}
        actions = {a.id: a for a in game.legal_actions(state)}
        ordered = tuple(actions[key] for key in case['request']['questions']['move']['criteria'])
        assert case['request'] == game.decision_request(state, ordered, prompt_variant='legacy')
        # The correction uses board order when scores tie, regardless of option order.
        assert game.session_policy.correct_action(game, state, 'place_D5', config,
                                                  case['tactical_evidence'])[0] == 'place_C1'


def test_resume_without_protocol_never_reads_live_strategy(qapp, tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('opponent:\n  prompt_variant: coached_quoted\n')
    monkeypatch.setattr(comparison, 'comparison_strategy', lambda *_: pytest.fail('Resume reread live rules'))
    monkeypatch.setattr(sys, 'argv', ['compare', '--config', str(config), '--output', str(tmp_path / 'missing'), '--resume'])
    with pytest.raises(SystemExit):
        comparison.main()
