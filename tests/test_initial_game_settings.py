import json

import pytest
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.storage.coached import CoachedStore, atomic_json
from app.ui.window import Window
from test_protocol import INK
from test_session import Fake, wait_for


def create_window(tmp_path, initial='coached_quoted'):
    root=tmp_path/'learning'
    ledger=CoachedStore(root).ledger()
    ledger.update(revision=4,strategy=['win immediately','otherwise stop X winning next turn'])
    atomic_json(root/'strategy.json',ledger)
    client=Fake()
    c=SessionController(TicTacToe(),client,Config(prompt_variant=initial,tactical_guard=False,
                                                learning_directory=root))
    return c,client,Window(c)


@pytest.mark.parametrize('initial,selected', [('coached_quoted','quoted'),('quoted','legacy'),('legacy','coached_quoted')])
def test_choose_first_game_mode_without_resetting_ink_or_starter(qapp,tmp_path,initial,selected):
    c,client,w=create_window(tmp_path,initial)
    wait_for(qapp,lambda:c.ready and not c.busy)
    game_id=c.session_id
    c.set_pending(INK)
    w.prompt_selector.setCurrentIndex(w.prompt_selector.findData(selected))
    w.move_temperature.setValue(.5)
    assert c.config.prompt_variant==selected and c.config.move_temperature==.5
    assert c.session_id==game_id and c.state.starting_player=='X' and c.state.revision==0
    assert c.pending==INK and len(client.calls)==1 and c.editable
    assert 'Click New game' not in w.prompt_label.text()
    assert ('coaching' in c.record())==(selected=='coached_quoted')
    assert c.strategy_revision==(4 if selected=='coached_quoted' else 0)
    assert c.strategy==(['win immediately','otherwise stop X winning next turn'] if selected=='coached_quoted' else [])
    c.submit()
    wait_for(qapp,lambda:not c.busy and c.state.revision==2)
    assert c.events[-1]['prompt_variant']==selected
    assert c.events[-1]['move_sampling']['temperature']==.5
    w.close()


def test_choose_mode_during_warmup_keeps_inflight_request(qapp,tmp_path):
    c,client,w=create_window(tmp_path)
    client.gate.clear()
    wait_for(qapp,lambda:c.busy and len(client.calls)==1)
    ticket=c.inflight
    game_id=c.session_id
    w.prompt_selector.setCurrentIndex(w.prompt_selector.findData('quoted'))
    assert c.config.prompt_variant=='quoted'
    assert c.inflight==ticket and c.session_id==game_id and c.busy
    client.gate.set()
    wait_for(qapp,lambda:c.ready and not c.busy)
    assert c.phase=='human' and c.state.revision==0 and len(client.calls)==1
    assert 'coaching' not in c.record()
    w.close()


def test_quoted_selected_before_first_move_never_coaches_after_loss(qapp,tmp_path):
    c,client,w=create_window(tmp_path)
    wait_for(qapp,lambda:c.ready and not c.busy)
    w.prompt_selector.setCurrentIndex(w.prompt_selector.findData('quoted'))
    c._coach=lambda:pytest.fail('Coaching ran in Quoted mode')
    c.set_pending(INK)
    c.submit()
    wait_for(qapp,lambda:not c.busy and c.state.revision==2)
    # The fake O takes B1 then C1; X wins the A1-B2-C3 diagonal.
    c.state=c.game.apply_action(c.state,'place_B2')
    c._after_move()
    wait_for(qapp,lambda:not c.busy and c.state.revision==4)
    c.state=c.game.apply_action(c.state,'place_C3')
    c._after_move()
    assert c.phase=='over' and c.game.outcome(c.state).winner=='X'
    assert c.learning_store.ledger()['revision']==4
    assert not list((tmp_path/'learning'/'games').glob('*.json'))
    assert all(e.get('prompt_variant')=='quoted' for e in c.events if e.get('ticket',{}).get('purpose')=='decision')
    w.close()


def test_selection_during_first_recognition_waits_for_new_game(qapp,tmp_path):
    c,client,w=create_window(tmp_path,'legacy')
    wait_for(qapp,lambda:c.ready and not c.busy)
    client.gate.clear()
    c.set_pending(INK)
    c.submit()
    wait_for(qapp,lambda:c.busy and len(client.calls)==2)
    w.prompt_selector.setCurrentIndex(w.prompt_selector.findData('quoted'))
    w.move_temperature.setValue(1)
    assert c.config.prompt_variant=='legacy' and c.config.move_temperature==0
    assert 'Click New game' in w.prompt_label.text()
    client.gate.set()
    wait_for(qapp,lambda:not c.busy and c.state.revision==2)
    assert c.events[-1]['prompt_variant']=='legacy' and 'move_sampling' not in c.events[-1]
    assert c.config.prompt_variant=='legacy'
    w.close()


def test_resumed_empty_game_retains_saved_settings(qapp,tmp_path):
    original=SessionController(TicTacToe(),Fake(),Config())
    path=tmp_path/'session.json'
    path.write_text(json.dumps(original.record()))
    c=SessionController(TicTacToe(),Fake(),Config())
    c.restore(path)
    w=Window(c)
    wait_for(qapp,lambda:c.ready and not c.busy)
    w.prompt_selector.setCurrentIndex(w.prompt_selector.findData('legacy'))
    w.move_temperature.setValue(1)
    assert c.config.prompt_variant=='quoted' and c.config.move_temperature==0
    assert 'Click New game' in w.prompt_label.text()
    w.close()
