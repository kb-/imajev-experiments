from dataclasses import replace
import json
import math
from pathlib import Path
import random

import pytest
from app.config import Config, load_config
from app.core.contracts import Scene, Stroke
from app.core.move_sampling import select_move
from app.core.session import SessionController
from app.games.boku.game import Boku, State
from app.games.boku.geometry import AXES, CELLS, CENTERS, DIRECTIONS, SPACING, geometry_matches, neighbor, recognition_view
from app.inference.imajev_client import parse_reply
from app.inference.opponents import from_imajev
from app.ui.rendering import observation_png
from app.ui.window import Window
from test_protocol import answer
from test_session import Fake, wait_for


game = Boku()


def ink(cell, symbol='O'):
    x, y = CENTERS[cell]
    r = .26*SPACING
    if symbol == 'X':
        return (Stroke(((x-r, y-r), (x+r, y+r)), .007),
                Stroke(((x+r, y-r), (x-r, y+r)), .007))
    return (Stroke(tuple((x+r*math.cos(i*math.tau/40), y+r*math.sin(i*math.tau/40)) for i in range(41)), .007),)


def capture_state():
    state = State()
    for c in ('A1', 'A2', 'K1', 'A3', 'A4'):
        state = game.apply_action(state, 'place_'+c)
    return state


def positioned(black=(), white=()):
    return replace(State(), board=tuple('Black' if c in black else 'White' if c in white else '' for c in CELLS),
                   reserves=(36-len(black), 36-len(white)))


def reply(state, cell, symbol='O', **options):
    request = game.recognition_request(state, ink(cell, symbol))
    return parse_reply(answer(request, {'symbol': symbol, 'cell': cell}, **options), request, 'imajev-2b')


def test_geometry():
    assert len(CELLS) == len(set(CENTERS.values())) == 80
    assert [sum(c.startswith(chr(65+r)) for c in CELLS) for r in range(11)] == [5,6,7,8,9,10,9,8,7,6,5]
    for c in CELLS:
        for dq, dr in DIRECTIONS:
            n = neighbor(c, (dq, dr))
            if n:
                assert neighbor(n, (-dq, -dr)) == c
                assert math.dist(CENTERS[c], CENTERS[n]) == pytest.approx(SPACING)
    assert sum(neighbor('F5', d) is not None for d in DIRECTIONS) == 6
    assert sum(neighbor('A1', d) is not None for d in DIRECTIONS) == 3


@pytest.mark.parametrize('axis', AXES)
@pytest.mark.parametrize('length', (5, 6))
def test_wins_and_overlines(axis, length):
    line = [{(1, 0): 'F1', (0, 1): 'A1', (1, -1): 'K1'}[axis]]
    for _ in range(length-1):
        line.append(neighbor(line[-1], axis))
    assert all(line)
    state = positioned(black=line)
    assert game.outcome(state).winner == 'Black'
    assert game.outcome(replace(state, board=tuple('White' if c in line else '' for c in CELLS))).winner == 'White'
    assert game.outcome(positioned(black=line[:4])).kind == 'ongoing'


def test_bent_chain_does_not_win():
    assert game.outcome(positioned(black=('F1','F2','F3','G3','H3'))).kind == 'ongoing'


@pytest.mark.parametrize('direction', DIRECTIONS)
def test_capture_each_direction(direction):
    target = 'F5'
    a, b, end = (neighbor(target, direction, i) for i in (1,2,3))
    assert end
    state = game.apply_action(positioned(black=(end,), white=(a,b)), 'place_'+target)
    assert set(state.capture_candidates) == {a,b}
    assert state.next_player == 'Black' and state.phase == 'capture'
    removed = game.apply_action(state, 'capture_'+a)
    assert removed.next_player == 'White' and removed.forbidden == a
    assert removed.reserves == (34,35)
    assert removed.board[CELLS.index(a)] == ''
    assert 'place_'+a not in {a.id for a in game.legal_actions(removed)}
    advanced = game.apply_action(removed, next(a.id for a in game.legal_actions(removed)))
    assert advanced.forbidden is None
    assert 'place_'+a in {a.id for a in game.legal_actions(advanced)}


def test_double_sandwich_removes_only_one():
    state = game.apply_action(positioned(black=('F2','F8'), white=('F3','F4','F6','F7')), 'place_F5')
    assert set(state.capture_candidates) == {'F3','F4','F6','F7'}
    state = game.apply_action(state, 'capture_F3')
    assert state.board.count('White') == 3 and state.reserves[1] == 33
    with pytest.raises(ValueError):
        game.apply_action(state, 'capture_F7')


@pytest.mark.parametrize('enemy,end', [(('F4',),'F5'), (('F3','F4','F5'),'F6')])
def test_only_exact_pair_is_captured(enemy, end):
    state = game.apply_action(positioned(black=(end,), white=enemy), 'place_F2')
    assert state.phase == 'placement' and not state.capture_candidates


def test_finish_capture_before_win_and_win_before_exhaustion():
    state = positioned(black=('F1','F2','F3','F4','I5'), white=('G5','H5'))
    state = replace(state, reserves=(1,34))
    state = game.apply_action(state, 'place_F5')
    assert state.phase == 'capture' and state.reserves[0] == 0
    assert game.outcome(state).kind == 'ongoing'
    state = game.apply_action(state, 'capture_G5')
    assert game.outcome(state).winner == 'Black'
    assert not game.legal_actions(state)
    exhausted = game.apply_action(replace(State(), reserves=(1,36)), 'place_A1')
    assert game.outcome(exhausted).kind == 'draw'


def test_replay_pending_capture_forbidden_terminal_and_tampering():
    state = capture_state()
    assert state.phase == 'capture'
    assert game.decode_state(json.loads(json.dumps(game.encode_state(state)))) == state
    state = game.apply_action(state, 'capture_A2', ink('A2','X'))
    assert game.decode_state(game.encode_state(state)) == state
    for field, value in [('reserves',[36,36]), ('forbidden',None), ('phase','capture'), ('capture_candidates',['A1']), ('starting_player','White'), ('revision',0)]:
        record = game.encode_state(state)
        record[field] = value
        with pytest.raises(ValueError):
            game.decode_state(record)
    terminal = State()
    for c in ('A1','K1','A2','K2','A3','K3','A4','K4','A5'):
        terminal = game.apply_action(terminal,'place_'+c)
    assert game.outcome(terminal).winner == 'Black'
    assert game.decode_state(game.encode_state(terminal)) == terminal


def test_recognition_conversion_and_snapshots(qapp):
    state = State()
    drawing = ink('F5')
    action = game.decode_recognition(state,drawing,reply(state,'F5'),.85)
    placed = game.apply_action(state, action, drawing)
    assert placed.history[0].drawing == drawing
    scene = game.render(placed,(), 'display')
    assert len(scene.stones) == 1 and not scene.strokes
    assert scene.stones[0].player == 'Black'
    assert not game.render(placed,drawing,'recognition').stones
    recognition = game.render(placed,drawing,'recognition')
    assert recognition.strokes and all(0 <= v <= 1 for s in recognition.strokes for p in s.points for v in p)
    assert not game.render(placed,drawing,'decision').strokes
    assert observation_png(scene).startswith(b'\x89PNG')
    state = capture_state()
    assert game.decode_recognition(state,ink('A2','X'),reply(state,'A2','X'),.85) == 'capture_A2'


@pytest.mark.parametrize('symbol,cell', [('X','F5'),('invalid','F5'),('O','invalid')])
def test_wrong_symbols_and_invalid_cells(symbol,cell):
    request = game.recognition_request(State(),())
    response = parse_reply(answer(request,{'symbol':symbol,'cell':cell}),request,'imajev-2b')
    with pytest.raises(ValueError):
        game.decode_recognition(State(),ink('F5'),response,.85)


def test_uncertainty_illegal_and_mislocated_marks():
    for options in ({'unknown':.2},{'abstained':True}):
        with pytest.raises(ValueError,match='confidently'):
            game.decode_recognition(State(),ink('F5'),reply(State(),'F5',**options),.85)
    assert not geometry_matches(ink('F5')+ink('F6'),'F5')
    assert not geometry_matches((), 'F5')
    assert not geometry_matches((Stroke(((float('nan'),.5),(.5,.5))),),'F5')
    with pytest.raises(ValueError,match='single pocket'):
        game.decode_recognition(State(),ink('F6'),reply(State(),'F5'),.85)
    state = game.apply_action(capture_state(),'capture_A2')
    state = replace(state,next_player='Black')
    for c in ('A2','A1'):
        with pytest.raises(ValueError,match='occupied or forbidden'):
            game.decode_recognition(state,ink(c),reply(state,c),.85)
    with pytest.raises(ValueError,match='highlighted'):
        game.decode_recognition(capture_state(),ink('K1','X'),reply(capture_state(),'K1','X'),.85)


def test_decisions_retries_sampling_and_bounded_history():
    state = replace(State(),next_player='White')
    actions = game.legal_actions(state)
    request = game.decision_request(state,actions)
    assert len(request['questions']['move']['criteria']) == 80
    response = from_imajev(parse_reply(answer(request,{'move':'place_F5'}),request,'imajev-2b'))
    assert select_move(game,state,response,.5,random.Random(1))[1] == 'place_F5'
    retry = game.retry_decision_request(state,actions,1)
    assert retry['state'] == request['state'] and retry['questions']['move']['criteria'] == request['questions']['move']['criteria']
    with pytest.raises(ValueError,match='abstained'):
        game.decode_decision(state,replace(response,abstained=True))
    capture = capture_state()
    capture_request = game.decision_request(capture,game.legal_actions(capture))
    choices = capture_request['questions']['move']['criteria']
    assert set(choices) == {'capture_A2','capture_A3'}
    assert capture_request['state']['pending_placement'] == 'A4'
    assert len(capture_request['state']['recent_turns']) == 4
    from app.games.boku.game import Move
    state = replace(state, history=tuple(Move('place_'+c,'Black') for c in CELLS[:20]))
    assert len(game.decision_request(state,actions)['state']['recent_turns']) == 12


class BokuFake(Fake):
    def __init__(self):
        super().__init__()
        self.cell = 'A1'
        self.symbol = 'O'
        self.wrong = False
    def decide(self, request, image):
        if request['state'].get('game') != 'boku' and 'F10' not in request['questions'].get('cell',{}).get('criteria',{}):
            return super().decide(request,image)
        self.calls.append(request)
        self.gate.wait(3)
        if 'symbol' in request['questions']:
            symbol = self.symbol
            choices = {'symbol':'invalid' if self.wrong else symbol,'cell':self.cell}
        else:
            choices = {'move':next(iter(request['questions']['move']['criteria']))}
        return parse_reply(answer(request,choices),request,'imajev-2b')


def controller(tmp_path):
    client = BokuFake()
    c = SessionController(game,client,Config(game='boku', prompt_variant='quoted',
                          learning_directory=tmp_path/'learning',directory=tmp_path/'sessions',save_sessions=True))
    c.ready = True
    c.new_game()
    return c, client


def test_controller_capture_flow_rejected_ink_and_resume(qapp,tmp_path):
    c,client = controller(tmp_path)
    assert not c.coached
    c.state = capture_state()
    c._after_move()
    assert 'Draw an X' in c.message
    client.cell = 'A2'
    client.symbol = 'X'
    client.wrong = True
    drawing = ink('A2','X')
    c.set_pending(drawing)
    c.submit()
    wait_for(qapp,lambda:not c.busy)
    assert c.state == capture_state() and c.pending == drawing
    client.wrong = False
    c.submit()
    wait_for(qapp,lambda:not c.busy and c.state.revision == 7)
    assert c.state.next_player == 'Black' and not c.pending
    assert c.events[-2]['accepted_action'] == 'capture_A2'
    assert c.events[-1]['accepted_action'].startswith('place_')
    path = tmp_path/'saved.json'
    path.write_text(json.dumps(c.record()))
    restored,_ = controller(tmp_path)
    restored.restore(path)
    assert restored.state == c.state and not restored.coached
    assert not (tmp_path/'learning').exists()


def test_gui_game_switch_before_play_fixed_starter_and_coaching_isolation(qapp,tmp_path):
    c,client = controller(tmp_path)
    w = Window(c)
    w.show()
    wait_for(qapp,lambda:not c.busy and c.ready)
    assert 'Black' in w.players_label.text() and w.prompt_selector.isEnabled()
    assert w.submit_button.isVisible() and w.undo_button.isVisible()
    old = c.session_id
    w.new_game()
    assert c.state.starting_player == 'Black' and c.session_id != old
    w.selector.setCurrentIndex(w.selector.findData('tic_tac_toe'))
    assert c.game.id == 'tic_tac_toe' and not c.coached
    w.selector.setCurrentIndex(w.selector.findData('boku'))
    assert c.game.id == 'boku' and not c.coached and c.strategy == []
    wait_for(qapp,lambda:not c.busy and c.editable)
    client.cell = 'A1'
    c.set_pending(ink('A1'))
    c.submit()
    wait_for(qapp,lambda:not c.busy and c.state.revision == 2)
    assert not c.pending and len(c.game.render(c.state,(),'display').stones) == 2
    w.selector.setCurrentIndex(w.selector.findData('tic_tac_toe'))
    assert c.game.id == 'boku' and 'New game' in w.selector.toolTip()
    w.close()


def test_config_profile():
    config = load_config(Path('config.boku.yaml'))
    assert config.game == 'boku' and config.threshold == .65


def test_draw_resize_and_block_input_until_recognition_finishes(qapp,tmp_path):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    from app.ui.canvas import Canvas
    c,client = controller(tmp_path)
    client.cell = 'F5'
    canvas = Canvas(c)
    canvas.drawing_changed.connect(c.set_pending)
    canvas.resize(700,700)
    canvas.show()
    qapp.processEvents()
    def point(p):
        rect = canvas.board_rect()
        return QPoint(round(rect.x()+p[0]*rect.width()),round(rect.y()+p[1]*rect.height()))
    points = [point(p) for p in ink('F5')[0].points]
    QTest.mousePress(canvas,Qt.MouseButton.LeftButton,pos=points[0])
    for p in points[1:]:
        QTest.mouseMove(canvas,p)
    QTest.mouseRelease(canvas,Qt.MouseButton.LeftButton,pos=points[-1])
    assert geometry_matches(c.pending,'F5')
    frozen = c.pending
    png = observation_png(game.render(c.state,c.pending,'recognition'))
    canvas.resize(900,720)
    qapp.processEvents()
    assert c.pending == frozen
    assert observation_png(game.render(c.state,c.pending,'recognition')) == png
    client.gate.clear()
    c.submit()
    wait_for(qapp,lambda:c.busy and len(client.calls)==1)
    QTest.mousePress(canvas,Qt.MouseButton.LeftButton,pos=point(CENTERS['A1']))
    QTest.mouseRelease(canvas,Qt.MouseButton.LeftButton,pos=point(CENTERS['A1']))
    assert c.pending == frozen and not canvas.current
    client.gate.set()
    wait_for(qapp,lambda:not c.busy and c.state.revision==2)
    assert c.state.board[CELLS.index('F5')] == 'Black' and not c.pending
    canvas.close()


def test_computer_capture_uses_second_question_before_human_turn(qapp,tmp_path):
    c,client = controller(tmp_path)
    c.state = replace(positioned(black=('A2','A3'),white=('A1','K1')),next_player='White')
    c._after_move()
    wait_for(qapp,lambda:not c.busy and c.state.revision==2)
    assert [r['state']['phase'] for r in client.calls] == ['placement','capture']
    assert c.events[0]['accepted_action'] == 'place_A4'
    assert c.events[1]['accepted_action'] == 'capture_A2'
    assert c.state.next_player == 'Black' and c.state.forbidden == 'A2'
    assert c.phase == 'human' and not c.coached


def test_boku_uncoached_loss_never_coaches(qapp,tmp_path):
    c,client = controller(tmp_path)
    c.state = positioned(black=('A1','A2','A3','A4','A5'))
    c._coach = lambda:pytest.fail('Boku used tic-tac-toe coaching')
    c._after_move()
    assert c.phase == 'over' and c.message == 'You won!' and not client.calls
    assert not (tmp_path/'learning').exists()


def test_recognition_view_frames_all_ink_without_selecting_a_cell():
    for drawing in (ink('A1'),ink('F5'),ink('K5'),ink('A1')+ink('K5')):
        left,top,size = recognition_view(drawing)
        assert 0 <= left <= 1-size and 0 <= top <= 1-size
        assert all(left <= x <= left+size and top <= y <= top+size
                   for stroke in drawing for x,y in stroke.points)
    assert recognition_view(ink('A1')+ink('K5'))[2] > recognition_view(ink('F5'))[2]
    state = State()
    request = game.recognition_request(state,ink('F5'))
    # Framing never narrows the model's choices to a guessed or legal location.
    assert set(request['questions']['cell']['criteria']) == set(CELLS)|{'invalid'}
    assert 'exactly one empty area' in request['questions']['symbol']['criteria']['O']
    assert 'Two circles are invalid' in request['questions']['symbol']['criteria']['invalid']
    assert game.render(state,ink('F5'),'recognition').protect_labels
    assert not game.render(state,ink('F5'),'display').protect_labels
    assert recognition_view(()) == (0,0,1)


def test_protected_label_stays_readable_when_ink_crosses_it(qapp):
    from PyQt6.QtGui import QFont, QFontMetrics, QImage
    label = Scene(labels=(('E2', .4, .5),), protect_labels=True)
    covered = replace(label, strokes=(Stroke(((.35,.49),(.5,.49)),.08),))
    reference = QImage.fromData(observation_png(label))
    protected = QImage.fromData(observation_png(covered))
    unprotected = QImage.fromData(observation_png(replace(covered,protect_labels=False)))
    font = QFont('Segoe UI'); font.setPixelSize(round(768*.023))
    bounds = QFontMetrics(font).boundingRect('E2').translated(round(.4*768),round(.5*768)).adjusted(-2,-2,2,2)
    assert protected.copy(bounds) == reference.copy(bounds)
    assert unprotected.copy(bounds) != reference.copy(bounds)


def test_resume_pending_capture_and_ink_then_warmup(qapp,tmp_path):
    c,client = controller(tmp_path)
    c.state = capture_state()
    c._after_move()
    c.set_pending(ink('A2','X'))
    path = tmp_path/'pending-capture.json'
    path.write_text(json.dumps(c.record()))
    restored,other = controller(tmp_path)
    restored.ready = False
    restored.restore(path)
    restored.start()
    wait_for(qapp,lambda:not restored.busy and restored.ready)
    assert restored.phase == 'human' and restored.state.phase == 'capture'
    assert restored.pending == c.pending and 'Draw an X' in restored.message
    assert restored.events[-1]['state']['phase'] == 'placement'  # Warm-up never acts on restored capture.
    other.cell = 'A2'
    other.symbol = 'X'
    restored.submit()
    wait_for(qapp,lambda:not restored.busy and restored.state.revision==7)
    assert restored.state.next_player == 'Black' and not restored.pending
