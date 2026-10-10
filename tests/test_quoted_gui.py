"""Quoted prompts travel through the actual GUI/controller and reply contract."""
import json
from pathlib import Path

import pytest
from app.config import Config, load_config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import State, TicTacToe
from app.games.tic_tac_toe.prompting import decision_request
from app.inference.imajev_client import parse_reply
from app.inference.opponents import from_imajev
from app.ui.window import Window
from test_protocol import INK, answer
from test_session import Fake, make, wait_for


def test_quoted_config_and_invalid_variant(tmp_path):
    assert load_config(Path('config.quoted.yaml')).prompt_variant == 'quoted'
    assert load_config(tmp_path / 'missing.yaml').prompt_variant == 'quoted'
    assert Config().prompt_variant == 'quoted'
    assert load_config(Path('config.yaml')).prompt_variant == 'quoted'
    assert load_config(Path('config.no-opening.yaml')).prompt_variant == 'legacy'
    path = tmp_path / 'invalid.yaml'
    path.write_text('opponent:\n  prompt_variant: thinking\n')
    with pytest.raises(ValueError, match='legacy, quoted or coached_quoted'):
        load_config(path)


@pytest.mark.parametrize('starter', ['X', 'O'])
def test_game_uses_exact_experiment_request(starter):
    game = TicTacToe()
    state = game.initial_state_for_player(starter)
    if starter == 'X':
        state = game.apply_action(state, 'place_A1')
    actions = game.legal_actions(state)
    expected = decision_request(state, actions, 'quoted')
    assert game.decision_request(state, actions, prompt_variant='quoted') == expected
    assert game.decision_request(state, actions, False, 'quoted') == expected
    retry = game.retry_decision_request(state, actions, 1, prompt_variant='quoted')
    second = game.retry_decision_request(state, actions, 2, prompt_variant='quoted')
    assert retry != expected and retry != second
    assert retry['questions']['move']['criteria'] == expected['questions']['move']['criteria']
    assert retry['state']['strategy'] == expected['state']['strategy']
    assert 'opening_advice' not in retry['state']
    assert 'place_' not in retry['questions']['move']['instructions']


def test_quoted_abstention_retry_and_resume(qapp, tmp_path):
    class Client(Fake):
        def decide(self, request, image):
            if 'move' not in request['questions']:
                return super().decide(request, image)
            self.calls.append(request)
            return parse_reply(answer(request, {'move': 'B2'}, abstained=len(self.calls) == 1), request, 'imajev-2b')

    game, client = TicTacToe(), Client()
    c = SessionController(game, client, Config(prompt_variant='quoted', tactical_guard=False))
    c.ready = True
    c.state = game.apply_action(State(), 'place_A1')
    c._launch('decision')
    wait_for(qapp, lambda: c.phase == 'error')
    assert c.state.revision == 1
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(c.record()))
    resumed = SessionController(game, Fake(), Config())
    resumed.restore(path)
    assert resumed.config.prompt_variant == 'quoted'
    assert resumed.decision_attempt == 1
    c.retry()
    wait_for(qapp, lambda: c.phase == 'human')
    assert c.state.board[4] == 'O'
    assert c.events[-1]['model_proposed_action'] == 'place_B2'
    assert c.events[-1]['accepted_action'] == 'place_B2'
    assert c.events[-1]['prompt_variant'] == 'quoted'
    assert c.events[-1]['prompt_version'] == 'prompting-strategy-v1:quoted'
    assert c.record()['prompt_variant'] == 'quoted'


def test_quoted_rejects_occupied_cell():
    game = TicTacToe()
    state = game.apply_action(State(), 'place_A1')
    # Reply contract can be valid for an older board; game validation remains authoritative.
    old = game.decision_request(game.initial_state_for_player('O'), game.legal_actions(game.initial_state_for_player('O')), prompt_variant='quoted')
    reply = parse_reply(answer(old, {'move': 'A1'}), old, 'imajev-2b')
    with pytest.raises(ValueError, match='invalid move'):
        game.decode_decision(state, from_imajev(reply))


def test_gui_switch_applies_on_new_game_and_accepts_bare_id(qapp):
    c, fake = make(qapp)
    c.config = Config(prompt_variant='legacy')
    window = Window(c)
    window.resize(880, 690)
    window.show()
    wait_for(qapp, lambda: not c.busy and len(fake.calls) == 1)
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: not c.busy and c.state.revision == 2)
    window.prompt_selector.setCurrentIndex(window.prompt_selector.findData('quoted'))
    assert c.config.prompt_variant == 'legacy'
    assert 'Click New game' in window.prompt_label.text()
    window.new_game()
    wait_for(qapp, lambda: c.phase == 'human' and c.state.revision == 1)
    assert c.state.starting_player == 'O'
    assert c.config.prompt_variant == 'quoted'
    request = fake.calls[-1]
    assert list(request['questions']['move']['criteria'])[0] == 'A1'
    assert c.events[-1]['accepted_action'] == 'place_A1'
    assert 'Playing: Quoted' in window.prompt_label.text()
    window.prompt_selector.setCurrentIndex(window.prompt_selector.findData('legacy'))
    window.new_game()
    assert c.config.prompt_variant == 'legacy'
    assert c.state.starting_player == 'X'
    window.close()


def test_gui_switch_during_inference_discards_old_prompt_reply(qapp):
    c, fake = make(qapp)
    c.config = Config(prompt_variant='legacy')
    window = Window(c)
    wait_for(qapp, lambda: not c.busy and len(fake.calls) == 1)
    fake.gate.clear()
    window.prompt_selector.setCurrentIndex(window.prompt_selector.findData('quoted'))
    window.new_game()
    wait_for(qapp, lambda: len(fake.calls) == 2)
    assert c.busy and 'A1' in fake.calls[-1]['questions']['move']['criteria']
    window.prompt_selector.setCurrentIndex(window.prompt_selector.findData('legacy'))
    window.new_game()
    fake.gate.set()
    wait_for(qapp, lambda: not c.busy)
    assert c.config.prompt_variant == 'legacy'
    assert c.phase == 'human' and c.state.revision == 0
    assert not c.events
    window.close()


def test_old_session_resumes_original_prompt(qapp, tmp_path):
    c, _ = make(qapp)
    record = c.record()
    del record['prompt_variant']
    path = tmp_path / 'old-session.json'
    path.write_text(json.dumps(record))
    resumed = SessionController(TicTacToe(), Fake(), Config())
    resumed.restore(path)
    assert resumed.config.prompt_variant == 'legacy'
