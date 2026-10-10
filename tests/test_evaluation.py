import json
import sys
import pytest
from app.config import Config
from app.games.tic_tac_toe.game import State, TicTacToe
from test_session import Fake
import scripts.evaluate as evaluator


from scripts.evaluate import oracle, o_turn_states

def test_minimax_oracle_is_separate_and_complete():
    assert oracle(('',) * 9, 'X') == 0
    assert oracle(('O', 'O', 'O', 'X', 'X', '', '', '', ''), 'X') == 1
    assert oracle(('X', 'X', 'X', 'O', 'O', '', '', '', ''), 'O') == -1
    assert len(list(o_turn_states())) == 2097


@pytest.mark.parametrize('mode', ['contract', 'opponent'])
@pytest.mark.parametrize('variant', ['quoted', 'legacy'])
def test_cli_evaluates_configured_prompt_and_reports_it(qapp, monkeypatch, tmp_path, mode, variant):
    config = tmp_path / 'config.yaml'
    config.write_text(f'opponent:\n  prompt_variant: {variant}\n  opening_suggestion: false\n')
    output = tmp_path / 'evaluation.json'
    client = Fake()
    monkeypatch.setattr(evaluator, 'ImajevClient', lambda config: client)
    argv = ['evaluate', '--config', str(config), '--output', str(output), mode]
    if mode == 'opponent':
        argv += ['--limit', '1']
    monkeypatch.setattr(sys, 'argv', argv)
    evaluator.main()
    report = json.loads(output.read_text())
    assert report['prompt_variant'] == variant
    assert report['opening_suggestion'] is False
    request = client.calls[-1]
    game = TicTacToe()
    state = game.apply_action(State(), 'place_A1')
    assert request == game.decision_request(state, game.legal_actions(state),
                                           opening_suggestion=False, prompt_variant=variant)
    if mode == 'contract':
        assert report['passed']
    else:
        assert report['failed_or_abstained'] == 0
        assert report['results'][0]['model_proposed'].startswith('place_')


def test_recognition_evaluation_retains_strict_reply_contract(qapp, tmp_path):
    from test_protocol import INK
    from dataclasses import asdict
    path = tmp_path / 'drawings.json'
    path.write_text(json.dumps({'version': 1, 'samples': [{'id': 'drawing', 'participant': 'test',
        'split': 'heldout', 'strokes': [asdict(stroke) for stroke in INK], 'valid_x': True, 'cell': 'A1'}]}))
    result = evaluator.recognition(path, 'heldout', Fake(), Config())
    assert result['results'][0]['accepted'] == 'place_A1'
    assert result['results'][0]['reply']['answers']['symbol']['choice'] == 'X'
