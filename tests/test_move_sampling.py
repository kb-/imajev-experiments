import json
import random
import pytest
from app.config import Config, load_config
from app.core.contracts import ChoiceAnswer, Reply
from app.core.move_sampling import select_move
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.imajev_client import parse_reply
from app.ui.window import Window, format_question_history
from test_protocol import INK, answer
from test_session import Fake, make, wait_for
from test_tactics import sequence


class NoSampling:
    def choices(self, *args, **kwargs):
        raise AssertionError('Sampling must not run here')


def reply(weights, abstained=False):
    return Reply('test', {'move': ChoiceAnswer(max(weights, key=weights.get), weights, .4, abstained)}, {})


def test_default_and_configured_temperature(tmp_path):
    assert Config().move_temperature == 0
    assert load_config(tmp_path / 'missing.yaml').move_temperature == 0
    path = tmp_path / 'varied.yaml'
    path.write_text('opponent:\n  move_temperature: 1.5\n')
    assert load_config(path).move_temperature == 1.5


@pytest.mark.parametrize('value', ['-1', '3.1', '.nan', '.inf', 'true', 'null', 'bad'])
def test_invalid_temperature(tmp_path, value):
    path = tmp_path / 'invalid.yaml'
    path.write_text(f'opponent:\n  move_temperature: {value}\n')
    with pytest.raises(ValueError, match='Move temperature'):
        load_config(path)


def test_zero_preserves_top_choice_without_sampling():
    game = TicTacToe()
    assert select_move(game, game.initial_state_for_player('O'), reply({'A1': .9, 'B1': .1}), 0, NoSampling()) == (
        'place_A1', 'place_A1', {})


@pytest.mark.parametrize('temperature,expected', [(1, .9), (2, .75), (.5, 81/82), (1e-300, 1)])
def test_temperature_changes_distribution_without_unknown_or_zero_choices(temperature, expected):
    game = TicTacToe()
    proposed, selected, probabilities = select_move(game, game.initial_state_for_player('O'),
        reply({'A1': .9, 'B1': .1, 'C1': 0}), temperature, random.Random(2))
    assert proposed == 'place_A1'
    assert selected in ('place_A1', 'place_B1')
    assert set(probabilities) == {'place_A1', 'place_B1'}
    assert probabilities['place_A1'] == pytest.approx(expected)
    assert sum(probabilities.values()) == pytest.approx(1)


def test_sampling_varies_moves_and_respects_preference():
    game, rng = TicTacToe(), random.Random(123)
    results = [select_move(game, game.initial_state_for_player('O'), reply({'A1': .9, 'B1': .1}), 1, rng)[1]
               for _ in range(1000)]
    assert .87 < results.count('place_A1') / len(results) < .93
    assert 'place_B1' in results


def test_abstention_and_invalid_top_choice_cannot_be_bypassed():
    game = TicTacToe()
    state = game.apply_action(game.initial_state(), 'place_A1')
    with pytest.raises(ValueError, match='abstained'):
        select_move(game, state, reply({'B2': 1}, abstained=True), 1, NoSampling())
    with pytest.raises(ValueError, match='invalid move'):
        select_move(game, state, reply({'A1': .9, 'B2': .1}), 1, NoSampling())
    _, selected, distribution = select_move(game, state, reply({'B2': .8, 'A1': .2}), 1, random.Random(2))
    assert selected == 'place_B2' and distribution == {'place_B2': 1}


class WeightedClient(Fake):
    def __init__(self, weights, abstain_once=False):
        super().__init__()
        self.weights, self.abstain_once = weights, abstain_once

    def decide(self, request, image):
        if 'move' not in request['questions']:
            return super().decide(request, image)
        self.calls.append(request)
        top = max(self.weights, key=self.weights.get)
        raw = answer(request, {'move': top}, abstained=self.abstain_once and len(self.calls) == 1)
        raw['answers']['move']['probabilities'] = {
            key: self.weights.get(key, 0) for key in request['questions']['move']['criteria']}
        return parse_reply(raw, request, 'imajev-2b')


def controller(client, **settings):
    c = SessionController(TicTacToe(), client, Config(move_temperature=1, **settings))
    c.ready = True
    c.move_rng.seed(2)  # First draw exceeds .9 and selects the second choice.
    return c


def test_controller_preserves_raw_reply_and_records_sampled_action(qapp):
    c = controller(WeightedClient({'A1': .9, 'B1': .1}), tactical_guard=False)
    c.state = c.game.initial_state_for_player('O')
    c._launch('decision')
    wait_for(qapp, lambda: not c.busy)
    event = c.events[-1]
    assert event['reply']['answers']['move']['choice'] == 'A1'
    assert event['model_proposed_action'] == 'place_A1'
    assert event['move_sampling']['selected_action'] == event['accepted_action'] == 'place_B1'
    assert c.state.board[1] == 'O' and c.state.board[0] == ''
    assert 'sampled' in c.last_move and 'Move sampling' in c.diagnostics
    assert 'selected place_B1' in format_question_history(c.events)


def test_tactical_guard_corrects_sampled_move_and_keeps_original_top_choice(qapp):
    c = controller(WeightedClient({'A1': .9, 'B3': .1}))
    c.state = sequence('B2', 'C2', 'C3')
    c._launch('decision')
    wait_for(qapp, lambda: not c.busy)
    event = c.events[-1]
    assert event['model_proposed_action'] == 'place_A1'
    assert event['move_sampling']['selected_action'] == 'place_B3'
    assert event['tactical_correction']['proposed'] == 'place_B3'
    assert event['accepted_action'] == 'place_A1'
    assert c.state.board[0] == 'O' and 'tactical rule' in c.last_move


def test_abstention_retry_samples_only_after_valid_response(qapp):
    c = controller(WeightedClient({'A1': .9, 'B1': .1}, abstain_once=True), tactical_guard=False)
    c.state = c.game.initial_state_for_player('O')
    c._launch('decision')
    wait_for(qapp, lambda: not c.busy)
    assert c.phase == 'error' and c.state.revision == 0
    assert 'move_sampling' not in c.events[-1]
    c.retry()
    wait_for(qapp, lambda: not c.busy)
    assert c.phase == 'human' and c.state.board[1] == 'O'
    assert c.events[-1]['decision_attempt'] == 1
    assert c.events[-1]['move_sampling']['temperature'] == 1


def test_forced_move_does_not_sample(qapp):
    c = controller(Fake())
    c.move_rng = NoSampling()
    c.state = c.game.initial_state_for_player('O')
    for cell in ('A1', 'B1', 'C1', 'B2', 'A2', 'A3', 'B3', 'C2'):
        c.state = c.game.apply_action(c.state, f'place_{cell}')
    c._launch('decision')
    assert c.phase == 'over' and c.game.outcome(c.state).kind == 'draw'
    assert not c.client.calls
    assert c.events[-1]['forced_action'] == 'place_C3'


def test_resume_preserves_temperature_and_old_sessions_default_off(qapp, tmp_path):
    c = controller(Fake(), tactical_guard=False)
    path = tmp_path / 'session.json'
    record = c.record()
    path.write_text(json.dumps(record))
    resumed = SessionController(TicTacToe(), Fake(), Config(move_temperature=2))
    resumed.restore(path)
    assert resumed.config.move_temperature == 1
    del record['move_temperature']
    path.write_text(json.dumps(record))
    resumed.restore(path)
    assert resumed.config.move_temperature == 0
    record['move_temperature'] = -1
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='Move temperature'):
        resumed.restore(path)


def test_gui_temperature_applies_to_next_game(qapp):
    c, fake = make(qapp)
    window = Window(c)
    window.resize(880, 690)
    window.show()
    wait_for(qapp, lambda: not c.busy and len(fake.calls) == 1)
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: not c.busy and c.state.revision == 2)
    window.move_temperature.setValue(1.5)
    assert c.config.move_temperature == 0
    assert 'Click New game' in window.variety_label.text()
    window.new_game()
    wait_for(qapp, lambda: not c.busy)
    assert c.config.move_temperature == c.record()['move_temperature'] == 1.5
    assert c.events[-1]['move_sampling']['temperature'] == 1.5
    assert 'Playing: temperature 1.5' in window.variety_label.text()
    window.move_temperature.setValue(0)
    window.new_game()
    assert c.config.move_temperature == 0
    window.close()
