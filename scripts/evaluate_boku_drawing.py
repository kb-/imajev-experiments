"""Replay saved Boku handwriting against image/prompt variants on an external service.

Does not alter gameplay, saved sessions, thresholds or model weights. Reuses the
explicitly selected service; the service busy gate serializes GPU requests.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import time
from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.core.contracts import Scene, Stroke
from app.games.boku.game import Boku
from app.games.boku.geometry import CELLS, CENTERS, SPACING, geometry_matches, recognition_view
from app.inference.imajev_client import ImajevClient, InferenceError
from app.storage.atomic import atomic_json
from app.ui.rendering import observation_png
from scripts.validate_boku import drawing as synthetic

VARIANTS = ('baseline','thin','labels','both','black','loop_words','no_grid','split','prompt','overlay','protected','outline','strict','selected')


def samples(paths):
    game = Boku()
    cases = []
    for path in paths:
        record = json.loads(path.read_text())
        for event in record['events']:
            if event.get('ticket',{}).get('purpose') != 'recognition' or not event.get('reply'):
                continue
            ink = tuple(Stroke(tuple(tuple(p) for p in s['points']),s['width'],s['color']) for s in event['drawing'])
            cells = [c for c in CELLS if geometry_matches(ink,c)]
            # Saved cases are manually inspected loops or the two-stroke X.
            symbol = 'X' if len(ink)==2 else 'O'
            if len(cells)!=1:
                raise ValueError('Saved drawing needs manual ground-truth annotation: '+str(path))
            cases.append({'id':event['ticket']['request_id'],'state':game.decode_state(event['state']),
                          'ink':ink,'symbol':symbol,'cell':cells[0], 'source':str(path),
                          'baseline':event['reply'], 'request':event['request']})
    return cases


def controls():
    game = Boku(); state = game.initial_state()
    x,y = CENTERS['F5']; d = .023
    capture = state
    for cell in ('A1','A2','K1','A3','A4'):
        capture = game.apply_action(capture,'place_'+cell)
    return [
        {'id':'blank','state':state,'ink':(),'symbol':'invalid','cell':None},
        {'id':'zigzag','state':state,'ink':(Stroke(((x-d,y-d),(x+d,y-d/2),(x-d,y),(x+d,y+d/2),(x-d,y+d))),), 'symbol':'invalid','cell':'F5'},
        {'id':'two_circles','state':state,'ink':synthetic('E2','O')+synthetic('E3','O'),'symbol':'invalid','cell':None},
        {'id':'two_loops_one_pocket','state':state,'ink':tuple(Stroke(tuple((x+offset+.009*math.cos(i*math.tau/32),y+.009*math.sin(i*math.tau/32)) for i in range(33)),.006) for offset in (-.015,.015)), 'symbol':'invalid','cell':'F5'},
        {'id':'dot','state':state,'ink':(Stroke(((x,y),)),),'symbol':'invalid','cell':'F5'},
        {'id':'straight_line','state':state,'ink':(Stroke(((x-d,y),(x+d,y))),),'symbol':'invalid','cell':'F5'},
        {'id':'open_arc','state':state,'ink':(Stroke(tuple((x+d*math.cos(i*math.pi/24),y+d*math.sin(i*math.pi/24)) for i in range(25))),),'symbol':'invalid','cell':'F5'},
        {'id':'dense_scribble','state':state,'ink':(Stroke(tuple((x+d*math.cos(i*2.4),y+d*math.sin(i*2.4)) for i in range(18))),),'symbol':'invalid','cell':'F5'},
        {'id':'angular_loop','state':state,'ink':(Stroke(((x-d,y+d),(x,y-d),(x+d,y+d),(x-d,y+d))),),'symbol':'O','cell':'F5'},
        {'id':'valid_capture_X','state':capture,'ink':synthetic('A2','X'),'symbol':'X','cell':'A2'},
        {'id':'valid_center_circle','state':state,'ink':synthetic('F5','O'),'symbol':'O','cell':'F5'},
        {'id':'valid_corner_circle','state':state,'ink':synthetic('K5','O'),'symbol':'O','cell':'K5'},
    ]


def scene_for(game, sample, variant):
    ink = sample['ink']
    if variant == 'selected':
        return game.render(sample['state'],ink,'recognition')
    if variant not in ('baseline','labels','prompt','protected','outline','strict'):
        ink = tuple(replace(s,width=.006) for s in ink)
    if variant in ('black','loop_words','no_grid','split'):
        ink = tuple(replace(s,color='#263e38') for s in ink)
    scene = replace(game.render(sample['state'],ink,'recognition'),protect_labels=False)
    if variant not in ('baseline','thin','prompt','split','overlay','protected','outline','strict'):
        _,_,size = recognition_view(ink)
        scene = replace(scene,labels=tuple((label,x,y-.025/size) for label,x,y in scene.labels))
    if variant == 'no_grid':
        scene = replace(scene,lines=())
    return scene


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path,action='append',default=[])
    parser.add_argument('--config',type=Path,default=Path('config.boku.yaml'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--variants',nargs='+',choices=VARIANTS,default=['thin','labels','both'])
    parser.add_argument('--ids',nargs='*')
    parser.add_argument('--controls',action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    app = QApplication([])
    game = Boku()
    config = replace(load_config(args.config),external_inference=True)
    client = ImajevClient(config)
    cases = samples(args.session) + (controls() if args.controls else [])
    if args.ids:
        cases = [c for c in cases if any(c['id'].startswith(prefix) for prefix in args.ids)]
    if not cases:
        raise ValueError('No cases selected')
    atomic_json(args.output/'fixtures.json',[{'id':c['id'],'symbol':c['symbol'],'cell':c['cell'],
                'state':game.encode_state(c['state']),'drawing':[{'points':s.points,'width':s.width,'color':s.color} for s in c['ink']],
                'source':c.get('source')} for c in cases])
    result = {'threshold':config.threshold,'rows':[],'error':None}
    def invoke(request,png):
        deadline = time.monotonic()+180
        while True:
            try:
                return client.decide(request,png)
            except InferenceError as exc:
                if 'previous GPU request' not in str(exc) or time.monotonic()>deadline:
                    raise
                time.sleep(1)
    def run(sample,variant):
        request = game.recognition_request(sample['state'],sample['ink'])
        if variant != 'selected':
            # Freeze the starting prompt so comparisons remain reproducible after
            # the winning experiment is incorporated into the application.
            request['questions']['symbol']['instructions'] = 'What is the handwritten symbol?'
            request['questions']['symbol']['criteria'] = {
                'O':'One handwritten circle.', 'X':'One handwritten cross.',
                'invalid':'Blank, scribble or multiple marks.'}
        scene = scene_for(game,sample,variant)
        if variant in ('loop_words','no_grid','split','prompt','overlay','protected'):
            request['questions']['symbol']['criteria']['O'] = 'One handwritten closed loop, including an imperfect circle, oval or angular loop.'
        if variant in ('outline','strict'):
            request['questions']['symbol']['instructions'] = 'Identify the structure of the handwritten ink, ignoring the printed grid and labels.'
            request['questions']['symbol']['criteria'] = {
                'O':'One handwritten outline enclosing one empty area. An uneven or angular closed loop counts.',
                'X':'Two handwritten diagonal lines crossing once.',
                'invalid':'Blank, scribble, multiple separate marks, or ink that forms neither a loop nor a cross.'}
        if variant == 'strict':
            request['questions']['symbol']['criteria'] = {
                'O':'Exactly one closed handwritten loop enclosing exactly one empty area. Uneven or angular loops count.',
                'X':'Exactly two handwritten diagonal lines crossing once.',
                'invalid':'Blank, a dot, an open line, scribble, or multiple loops or marks. Two circles are invalid even in the same cell.'}
        png = observation_png(scene,config.observation_size)
        if variant in ('overlay','split','protected','outline','strict'):
            from PyQt6.QtGui import QImage, QPainter, QFont, QColor
            from PyQt6.QtCore import QPointF
            image = QImage.fromData(png)
            painter = QPainter(image)
            font = QFont('Segoe UI'); font.setPixelSize(max(10,round(config.observation_size*.023)))
            painter.setFont(font)
            for label,x,y in scene.labels:
                point = QPointF(x*config.observation_size,y*config.observation_size)
                bounds = painter.fontMetrics().boundingRect(label).translated(round(point.x()),round(point.y())).adjusted(-2,-2,2,2)
                painter.fillRect(bounds,QColor(scene.background))
                painter.setPen(QColor(scene.label_color)); painter.drawText(point,label)
            painter.end()
            from PyQt6.QtCore import QByteArray,QBuffer,QIODevice
            data=QByteArray(); buffer=QBuffer(data);buffer.open(QIODevice.OpenModeFlag.WriteOnly);image.save(buffer,'PNG');png=bytes(data)
        (args.output/(sample['id']+'-'+variant+'.png')).write_bytes(png)
        started = time.monotonic()
        if variant == 'split':
            symbol_request = {'state':{'image_content':'A handwritten mark.'},'questions':{'symbol':request['questions']['symbol']}}
            points = [point for stroke in sample['ink'] for point in stroke.points]
            if points:
                left,right = min(p[0] for p in points),max(p[0] for p in points)
                top,bottom = min(p[1] for p in points),max(p[1] for p in points)
                size = max(.12,1.8*max(right-left,bottom-top))
                left,top = (left+right-size)/2,(top+bottom-size)/2
                symbol_scene = Scene(strokes=tuple(Stroke(tuple(((x-left)/size,(y-top)/size) for x,y in s.points),
                                           .006/size,'#263e38') for s in sample['ink']))
            else:
                symbol_scene = Scene()
            (args.output/(sample['id']+'-'+variant+'-symbol.png')).write_bytes(observation_png(symbol_scene,config.observation_size))
            symbol_reply = invoke(symbol_request,observation_png(symbol_scene,config.observation_size))
            cell_request = {'state':request['state'],'questions':{'cell':request['questions']['cell']}}
            cell_reply = invoke(cell_request,png)
            from app.core.contracts import Reply
            reply = Reply(symbol_reply.model,{**symbol_reply.answers,**cell_reply.answers},
                          {'symbol_response':symbol_reply.raw,'cell_response':cell_reply.raw})
        else:
            reply = invoke(request,png)
        answers = {key:{'choice':a.choice,'effective':a.effective_probability,'unknown':a.unknown_probability,'abstained':a.abstained} for key,a in reply.answers.items()}
        expected_action = (f'{"capture" if sample["state"].phase == "capture" else "place"}_{sample["cell"]}'
                           if sample['symbol'] == ('X' if sample['state'].phase=='capture' else 'O') and sample['cell'] else None)
        try:
            accepted = game.decode_recognition(sample['state'],sample['ink'],reply,config.threshold)
            rejection = None
        except ValueError as exc:
            accepted, rejection = None,str(exc)
        return {'id':sample['id'],'variant':variant,'expected_symbol':sample['symbol'],'expected_cell':sample['cell'],
                'expected_action':expected_action,'accepted':accepted,'correct_acceptance':accepted==expected_action,
                'symbol_correct':reply.answers['symbol'].choice==sample['symbol'],
                'cell_correct':reply.answers['cell'].choice==sample['cell'] if sample['cell'] else None,
                'answers':answers,'rejection':rejection,'seconds':time.monotonic()-started,'request':request,'reply':reply.raw}
    try:
        for variant in args.variants:
            for sample in cases:
                row = run(sample,variant)
                result['rows'].append(row)
                atomic_json(args.output/'results.json',result)
                print(json.dumps({k:row[k] for k in ('id','variant','answers','accepted','correct_acceptance','seconds')}),flush=True)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        atomic_json(args.output/'results.json',result)
    return app


if __name__=='__main__':
    main()
