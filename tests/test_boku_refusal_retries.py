"""The retained v5 refusal pauses play; a retry never resigns the game."""
import json
from pathlib import Path

import pytest

from app.games.boku.game import Boku
from app.inference.imajev_client import parse_reply
from test_game_separation import make_controller
from test_session import wait_for


FIXTURES = Path(__file__).resolve().parents[1] / 'docs/evaluation/fixtures/boku'


def recorded_position():
    saved = json.loads((FIXTURES / 'abstained-loss-session.json').read_text())
    event = next(e for e in saved['events'] if e['ticket']['state_revision'] == 32)
    return Boku().decode_state(event['state']), event


def test_retained_prompt_reproduces_original_refusal_without_resignation():
    game = Boku()
    state, event = recorded_position()
    request = game.decision_request(state, game.legal_actions(state), prompt_variant='quoted')
    assert json.loads(json.dumps(request)) == event['request']
    assert game.prompt_version == 'boku-v5-forced-win-defence'
    assert 'acknowledge_loss' not in request['questions']['move']['criteria']
    with pytest.raises(ValueError, match='Illegal Boku action'):
        game.apply_action(state, 'acknowledge_loss')
    assert game.outcome(state).kind == 'ongoing'


def test_recorded_refusal_and_retry_preserve_board_until_legal_play(qapp, tmp_path):
    rows = json.loads((FIXTURES / 'boku-refusal-retries.json').read_text())['rows']
    controller = make_controller(tmp_path, 'quoted')
    state, _ = recorded_position()
    controller.state = state

    class RecordedClient:
        def __init__(self):
            self.calls = 0

        def decide(self, request, png):
            row = rows[self.calls]
            self.calls += 1
            assert json.loads(json.dumps(request)) == row['request']
            return parse_reply(row['reply'], request, row['reply']['model'])

    controller.client = RecordedClient()
    controller._launch('decision')
    wait_for(qapp, lambda: not controller.busy)
    assert controller.phase == 'error'
    assert controller.state == state
    assert controller.game.outcome(controller.state).kind == 'ongoing'
    controller.retry()
    wait_for(qapp, lambda: not controller.busy)
    assert controller.phase == 'human'
    assert controller.state.history[-1].action == 'place_G7'
    assert controller.state.revision == state.revision + 1
    assert controller.game.outcome(controller.state).kind == 'ongoing'
    # G7 continues a lost game; only Black's actual G8 ends it.
    terminal = controller.game.apply_action(controller.state, 'place_G8')
    assert controller.game.outcome(terminal).winner == 'Black'
