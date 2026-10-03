from dataclasses import replace
import json
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from test_protocol import INK
from test_session import Fake, wait_for


def test_records_and_observations_replay(qapp, tmp_path):
    config = replace(Config(), save_sessions=True, directory=tmp_path)
    c = SessionController(TicTacToe(), Fake(), config)
    c.ready = True
    c.new_game()
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: c.phase == 'human' and c.state.revision == 2)
    directory = tmp_path / c.session_id
    record = json.loads((directory / 'session.json').read_text())
    assert record['version'] == 1
    assert c.game.decode_state(record['state']) == c.state
    assert len(record['events']) == 2
    for event in record['events']:
        assert (directory / event['image']).read_bytes().startswith(b'\x89PNG')
        assert event['ticket']['session_id'] == c.session_id
        assert event['accepted_action']


def test_storage_failure_does_not_interrupt_game(qapp, tmp_path):
    root = tmp_path / 'not-a-directory'
    root.write_text('occupied')
    c = SessionController(TicTacToe(), Fake(), replace(Config(), save_sessions=True, directory=root))
    c.ready = True
    c.new_game()
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: c.phase == 'human' and c.state.revision == 2)
    assert c.storage_error
