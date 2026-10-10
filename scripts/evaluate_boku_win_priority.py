"""Test explicit wins_now priority on saved Boku prompts without changing the GUI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.games.boku.game import Boku, State
from app.games.boku.geometry import CELLS
from app.games.boku.tactics import completions
from app.inference.imajev_client import ImajevClient, InferenceError
from app.storage.atomic import atomic_json
from app.ui.rendering import observation_png

WIN_INSTRUCTION = 'If any candidate has wins_now=true, choose one of those candidates.'
NAMED_WIN_INSTRUCTION = 'If immediate_White_win_actions is nonempty, choose one action from that list before considering any other action.'
ARMS = ('baseline','explicit_win_priority','prefix_win_priority','named_wins','plain_win_result')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, default=Path('docs/evaluation/fixtures/boku/missed-win-session.json'))
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    parser.add_argument('--output', type=Path, default=Path('logs/boku-win-priority.json'))
    parser.add_argument('--controls', action='store_true')
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=['baseline','explicit_win_priority'])
    args = parser.parse_args()
    app = QApplication([])
    game = Boku()
    record = json.loads(args.session.read_text())
    cases = []
    for revision, name in ((53,'missed_A1_win'),(9,'E5_block'),(25,'capture_trap_defence'),
                           (28,'F4_counter'),(29,'E4_capture')):
        if revision != 53 and not args.controls:
            continue
        event = next(e for e in record['events'] if e.get('ticket',{}).get('purpose')=='decision'
                     and e['ticket']['state_revision']==revision)
        state = game.decode_state(event['state'])
        cases.append((name,state,event['request']))
    if args.controls:
        white = ('F1','F2','F3','F4','I5')
        state = State(board=tuple('White' if c in white else 'Black' if c in ('G5','H5') else '' for c in CELLS),
                      next_player='White',reserves=(34,1))
        for action, name in ((None,'win_requires_capture'),('place_F5','winning_capture_choice')):
            probe = game.apply_action(state,action) if action else state
            cases.append((name,probe,game.decision_request(probe,game.legal_actions(probe),prompt_variant='quoted')))
    client = ImajevClient(replace(load_config(args.config),external_inference=True))
    result = {'instruction':WIN_INSTRUCTION,'source':str(args.session),'rows':[],'error':None}
    try:
        for name, state, original in cases:
            # Reproduce the pre-integration baseline even for newly built controls.
            original = json.loads(json.dumps(original))
            original['state'].pop('immediate_White_win_actions', None)
            original['questions']['move']['instructions'] = original['questions']['move']['instructions'].replace(
                ' '+NAMED_WIN_INSTRUCTION, '')
            # Independently establish winning legal actions, including captures.
            wins = {a.id for a in game.legal_actions(state)
                    if any(game.outcome(end).winner=='White' for _,end in completions(game,state,a.id))}
            for arm in args.arms:
                if arm=='baseline' and name!='missed_A1_win':
                    continue
                request = json.loads(json.dumps(original))
                if arm=='explicit_win_priority':
                    request['questions']['move']['instructions'] += ' '+WIN_INSTRUCTION
                elif arm=='prefix_win_priority':
                    request['questions']['move']['instructions'] = WIN_INSTRUCTION+' '+request['questions']['move']['instructions']
                elif arm=='named_wins':
                    request['state']['immediate_White_win_actions'] = sorted(wins)
                    request['questions']['move']['instructions'] += ' '+NAMED_WIN_INSTRUCTION
                elif arm=='plain_win_result':
                    for action in wins:
                        request['questions']['move']['criteria'][action]['result'] = 'White wins the game immediately by completing five or more stones in a straight line after any mandatory capture.'
                    request['questions']['move']['instructions'] += ' '+WIN_INSTRUCTION
                then=time.monotonic();deadline=then+180
                while True:
                    try:
                        reply=client.decide(request,observation_png(game.render(state,(),'decision')))
                        break
                    except InferenceError as exc:
                        if 'previous GPU request' not in str(exc) or time.monotonic()>deadline:
                            raise
                        time.sleep(1)
                selected=game.decode_decision(state,reply)
                facts=request['questions']['move']['criteria'][selected]
                passed=selected in wins if wins else not (
                    facts.get('allows_Black_win_next_turn',False) or facts.get('allows_Black_capture_forced_win',False))
                row={'case':name,'arm':arm,'wins':sorted(wins),'selected':selected,'passed':passed,
                     'request':request,'reply':reply.raw,'seconds':time.monotonic()-then}
                result['rows'].append(row)
                atomic_json(args.output,result)
                print(json.dumps({k:row[k] for k in ('case','arm','wins','selected','passed','seconds')}),flush=True)
    except Exception as exc:
        result['error']=str(exc)
        raise
    finally:
        atomic_json(args.output,result)
    return app


if __name__=='__main__':
    main()
