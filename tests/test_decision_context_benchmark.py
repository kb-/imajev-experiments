import json
from pathlib import Path

import pytest

from app.core.contracts import MoveResult
from app.games.tic_tac_toe.game import State, TicTacToe
from scripts import evaluate_decision_context as benchmark


def baseline():
    return json.loads(benchmark.DEFAULT_BASELINE.read_text())


def test_archived_requests_preserve_complete_context_and_legal_choice_orders():
    source = baseline()
    before = json.dumps(source)
    for case in source['cases']:
        request = benchmark.archived_request(source, case)
        assert list(request['questions']['move']['criteria']) == case['option_order']
        assert request['state']['definitions_and_rules'] == source['request_template']['state']['definitions_and_rules']
        assert request['state']['strategy'] == source['request_template']['state']['strategy']
        assert 'oracle_scores' not in request['state']
        assert all(text == f'Place your O mark in empty cell {action.removeprefix("place_")}.'
                   for action, text in request['questions']['move']['criteria'].items())
    assert json.dumps(source) == before


@pytest.mark.parametrize('failure', [None, 'inference', 'cleanup'])
def test_benchmark_scores_raw_moves_and_cleans_up_on_failure(tmp_path, monkeypatch, failure):
    source = baseline()
    source_path = tmp_path / 'baseline.json'
    source_path.write_text(json.dumps(source))
    output = tmp_path / 'report.json'
    trace = []
    monkeypatch.setattr(benchmark, 'active_inference', lambda _: [])
    class Manager:
        def __init__(self, config):
            assert not config.tactical_guard and config.move_temperature == 0
        def shutdown(self):
            trace.append('shutdown')
            if failure == 'cleanup': raise RuntimeError('cleanup unconfirmed')
    class Backend:
        def choose_move(self, request):
            trace.append('inference')
            if failure == 'inference': raise RuntimeError('capacity failure')
            board = tuple(mark if mark != '.' else '' for row in request['state']['board'] for mark in row)
            state = State(board=board, next_player='O')
            scores = benchmark.scores_for(TicTacToe(), state)
            choice = max(scores, key=scores.get)
            return MoveResult(choice, 'test', {'model': 'test'}, 'llama_cpp')
    monkeypatch.setattr(benchmark, 'ServiceManager', Manager)
    monkeypatch.setattr(benchmark, 'create_opponent', lambda *_, **__: Backend())
    if failure:
        with pytest.raises(RuntimeError):
            benchmark.evaluate(Path('configs/decision3-mini-4b-q8.yaml'), source_path, output, 1, 1)
    else:
        report = benchmark.evaluate(Path('configs/decision3-mini-4b-q8.yaml'), source_path, output, 1, 1)
        assert report['summary']['canonical_optimal'] == 1
        assert report['summary']['optimal_choices'] == 3
        assert report['summary']['full_games'] == 2
        assert report['summary']['draws'] == 2
        for row, old in zip(report['cases'], source['cases'][:3]):
            assert row['request'] == benchmark.archived_request(source, old)
    report = json.loads(output.read_text())
    assert trace[-1] == 'shutdown'
    assert report['experiment_completed'] == (failure is None)
    if failure == 'cleanup':assert report['cleanup_error'] == 'cleanup unconfirmed'
    else:assert report['cleanup_confirmed']
