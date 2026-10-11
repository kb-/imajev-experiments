"""Boku guard evidence is local, bounded and applied after the model proposal."""
import pytest

from app.config import Config
from app.core.session import SessionController
from app.games.boku.game import Boku, State
from app.games.boku.coaching import BASIC_QUOTED_STRATEGY
from app.games.boku.geometry import CELLS
from app.games.boku.policy import Policy
from app.inference.imajev_client import parse_reply
from test_boku import BokuFake
from test_boku_forced_wins import before_open_four
from test_protocol import answer
from test_session import wait_for


game = Boku()
policy = Policy()


def prepared(state, mode='quoted', guard=True):
    config = Config(game='boku', prompt_variant=mode, tactical_guard=guard)
    actions = game.legal_actions(state)
    request, evidence = policy.prepare_decision(game, state, actions, config, BASIC_QUOTED_STRATEGY, 0)
    return config, request, evidence


@pytest.mark.parametrize('mode', ['legacy', 'quoted'])
def test_unsafe_forced_setup_is_corrected_without_changing_legacy_prompt(mode):
    state = before_open_four()
    config, request, evidence = prepared(state, mode)
    if mode == 'legacy':
        assert request == game.decision_request(state, game.legal_actions(state), prompt_variant='legacy')
        assert 'White_defensive_actions' not in request['state']
    assert evidence['winning_actions'] == []
    assert evidence['defensive_actions'] == ['place_C1', 'place_C5', 'place_E1']
    chosen, reason = policy.correct_action(game, state, 'place_D5', config, evidence,
                                           {'place_C1': .1, 'place_C5': .8, 'place_E1': .2})
    assert chosen == 'place_C5' and 'forced-win' in reason
    assert policy.correct_action(game, state, 'place_E1', config, evidence)[0] == 'place_E1'
    assert policy.correct_action(game, state, 'place_D5', config, evidence)[0] == 'place_C1'


def test_guard_prioritizes_immediate_white_win():
    board = tuple('White' if c in ('A1', 'A2', 'A3', 'A4') else '' for c in CELLS)
    state = State(board=board, next_player='White', reserves=(36, 32))
    config, _, evidence = prepared(state)
    assert 'place_A5' in evidence['winning_actions']
    chosen, reason = policy.correct_action(game, state, 'place_B1', config, evidence)
    assert chosen == 'place_A5' and reason == 'take immediate White win'
    assert game.outcome(game.apply_action(state, chosen)).winner == 'White'


def test_guard_protects_mandatory_capture_choice():
    board = tuple('Black' if c in ('E6', 'E7', 'E8', 'E9', 'F8') else '' for c in CELLS)
    state = State(board=board, next_player='White', phase='capture',
                  capture_candidates=('E7', 'F8'), reserves=(31, 36))
    config, _, evidence = prepared(state)
    assert evidence['defensive_actions'] == ['capture_E7']
    assert policy.correct_action(game, state, 'capture_F8', config, evidence)[0] == 'capture_E7'


def test_guard_keeps_choice_when_all_checked_actions_lose():
    state = game.apply_action(before_open_four(), 'place_D5')
    state = game.apply_action(state, 'place_C5')
    config, _, evidence = prepared(state)
    assert evidence == {'winning_actions': [], 'defensive_actions': []}
    action = game.legal_actions(state)[0].id
    assert policy.correct_action(game, state, action, config, evidence) == (action, None)


@pytest.mark.parametrize('mode,guard', [('quoted', False), ('legacy', False), ('coached_quoted', True)])
def test_guard_is_absent_when_disabled_or_coached(mode, guard):
    state = before_open_four()
    config, _, evidence = prepared(state, mode, guard)
    assert evidence is None
    assert policy.correct_action(game, state, 'place_D5', config, evidence) == ('place_D5', None)


class UnsafeBokuFake(BokuFake):
    def decide(self, request, image):
        self.calls.append(request)
        return parse_reply(answer(request, {'move': 'place_D5'}), request, 'imajev-2b')


def test_controller_records_original_proposal_and_guard_correction(qapp, tmp_path):
    client = UnsafeBokuFake()
    config = Config(game='boku', prompt_variant='legacy', directory=tmp_path)
    controller = SessionController(game, client, config)
    controller.ready = True
    controller.state = before_open_four()
    controller._launch('decision')
    wait_for(qapp, lambda: not controller.busy, timeout=10)
    event = controller.events[-1]
    assert event['request'] == client.calls[-1]
    assert 'White_defensive_actions' not in event['request']['state']
    assert event['model_proposed_action'] == 'place_D5'
    assert event['accepted_action'] in ('place_C1', 'place_C5', 'place_E1')
    assert event['tactical_correction']['proposed'] == 'place_D5'
    assert event['tactical_correction']['committed'] == event['accepted_action']
    assert controller.record()['events'][-1]['tactical_evidence'] == event['tactical_evidence']
