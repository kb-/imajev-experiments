"""Compare loss-coach context variants and replay their rules through Imajev.

Uses an isolated output directory; never commits to the gameplay learning ledger.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time
from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.games.tic_tac_toe.game import TicTacToe, CELLS
from app.games.tic_tac_toe.coaching import enrich_game
from app.inference.coach import shared_coach, coach_pipeline, prepared_transport
from app.inference.imajev_client import ImajevClient
from app.inference.service_manager import ServiceManager
from app.storage.coached import CoachedStore
from app.games.tic_tac_toe.policy import Coaching as TicTacToeCoaching
from app.games.tic_tac_toe.coaching import BASIC_QUOTED_STRATEGY
from app.storage.atomic import atomic_json
from app.ui.rendering import observation_png
from scripts.evaluate import oracle

GAME=TicTacToe()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-id',required=True)
    parser.add_argument('--backend', choices=['shared','ollama'], default='shared')
    parser.add_argument('--model', default='qwen3.5:latest')
    variants_group=parser.add_mutually_exclusive_group()
    variants_group.add_argument('--minimal-only', action='store_true')
    variants_group.add_argument('--diagnose-first', action='store_true', help='Diagnose with explicit boards/lines, then update and replay one strategy.')
    parser.add_argument('--history',type=Path,default=Path('learning/coached-quoted'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    app=QApplication([])
    config=replace(load_config(Path('config.coached-quoted.yaml')),coach_backend=args.backend,coach_model=args.model)
    manager=ServiceManager(config); client=ImajevClient(config); client.manager=manager
    from app.games.tic_tac_toe.coaching import coaching_context
    original=CoachedStore(args.history, TicTacToeCoaching()).request(args.game_id)
    original['coach_context'] = coaching_context()
    enriched=dict(original,games=[enrich_game(g) if g['game_id']==args.game_id else g for g in original['games']])
    result={'backend':args.backend,'model':args.model if args.backend=='ollama' else config.expected_model,'game_id':args.game_id,'coaching':{},'positions':[],'games':[]}
    start=time.monotonic()
    def save(): atomic_json(args.output/'results.json',result)
    def record(stage):
        with (args.output/'coaching.jsonl').open('a') as stream: stream.write(json.dumps(stage,ensure_ascii=False)+'\n')
    def decide(state,rules,label):
        request=GAME.decision_request(state,GAME.legal_actions(state),prompt_variant='coached_quoted',strategy=rules)
        before=time.monotonic(); reply=client.decide(request,observation_png(GAME.render(state,(),'decision'),768)); elapsed=time.monotonic()-before
        answer=reply.answers['move']; choice='place_'+answer.choice
        scores={a.id:oracle((after:=GAME.apply_action(state,a.id)).board,after.next_player) for a in GAME.legal_actions(state)}
        value={'choice':choice,'abstained':answer.abstained,'correct':not answer.abstained and scores[choice]==max(scores.values()),'seconds':elapsed}
        with (args.output/'calls.jsonl').open('a') as stream:
            stream.write(json.dumps({'label':label,'state':GAME.encode_state(state),'request':request,'reply':reply.raw,'score':value})+'\n')
        return value
    try:
        manager.start(); state=GAME.initial_state(); client.warmup(GAME.recognition_request(state,()),observation_png(GAME.render(state,(),'recognition'),768))
        strategies={'basic':BASIC_QUOTED_STRATEGY}
        minimal = dict(enriched, games=[dict(g, result_for_O='draw' if g['outcome']=='draw' else 'loss' if g['winner']=='X' else 'win') for g in enriched['games']], revision_instruction='Return the previous strategy with at most one evidence-based rule addition or edit. Keep unaffected rules and their priority order exactly; do not replace the whole list with a generic strategy. Learn one general lesson from the avoidable mistake, rather than the already lost final turn.', winning_lines=[['A1','B1','C1'],['A2','B2','C2'],['A3','B3','C3'],['A1','A2','A3'],['B1','B2','B3'],['C1','C2','C3'],['A1','B2','C3'],['C1','B2','A3']])
        variants = [('analyzed_lines', minimal)] if args.diagnose_first else [('minimal_lines', minimal)] if args.minimal_only else [('focused_history',original),('focused_boards',enriched),('analyzed_boards',enriched)]
        def generate_all(invoke):
            for name,request in variants:
                before=time.monotonic()
                if name.startswith('analyzed_'):
                    diagnostic={'task':'diagnose','games':[request['games'][0]],'coach_context':request['coach_context']}
                    if request.get('winning_lines'):
                        diagnostic['winning_lines']=request['winning_lines']
                    response=invoke(diagnostic); record({'arm':name,'request':diagnostic,'response':response})
                    request=dict(request,loss_analysis=response['summary'])
                    result['coaching'][name]={'analysis':response}
                    save(); print('ANALYSIS '+name+' '+response['summary'],flush=True)
                response=coach_pipeline(request,invoke,record,7500 if args.backend=='ollama' else 9000)
                result['coaching'].setdefault(name,{}).update(response=response,seconds=time.monotonic()-before)
                strategies[name]=response['strategy']; save()
                print('COACH '+name+' '+json.dumps(strategies[name]),flush=True)
        if args.backend=='ollama':
            manager.ollama(lambda transport: generate_all(prepared_transport(transport, manager.progress)))
            client.warmup(GAME.recognition_request(state,()),observation_png(GAME.render(state,(),'recognition'),768))
        else:
            generate_all(lambda request:shared_coach(client,request))
        frozen=json.loads(Path('data/evaluation/prompting-strategy-positions.json').read_text())['positions'][:11]
        source=json.loads((args.history/'sessions'/args.game_id/'session.json').read_text())
        target=next(e['state'] for e in source['events'] if e.get('ticket',{}).get('purpose')=='decision' and e['ticket']['state_revision']==4)
        cases=[{'id':'trigger-preventable-mistake','state':target}]+frozen
        for case in cases:
            state=GAME.decode_state(case['state']); row={'id':case['id'],'state':case['state'],'arms':{}}
            for name,rules in strategies.items(): row['arms'][name]=decide(state,rules,case['id']+':'+name)
            result['positions'].append(row); save(); print('POSITION '+json.dumps({'id':case['id'],'arms':row['arms']}),flush=True)
        order=['place_C3','place_B1','place_C1','place_A1','place_B2','place_A3','place_A2','place_C2','place_B3']
        for name,rules in strategies.items():
            state=GAME.initial_state_for_player('O'); moves=[]; aborted=False
            while GAME.outcome(state).kind=='ongoing':
                actions=GAME.legal_actions(state)
                if state.next_player=='X':
                    scores={a.id:oracle((after:=GAME.apply_action(state,a.id)).board,after.next_player) for a in actions}
                    action=next(a for a in order if scores.get(a)==min(scores.values()))
                elif len(actions)==1: action=actions[0].id
                else:
                    decision=decide(state,rules,'game:'+name+':'+str(state.revision))
                    if decision['abstained']: aborted=True; break
                    action=decision['choice']
                moves.append({'player':state.next_player,'action':action}); state=GAME.apply_action(state,action)
            outcome=GAME.outcome(state)
            result['games'].append({'arm':name,'moves':moves,'winner':outcome.winner,'outcome':outcome.kind,'aborted':aborted}); save()
            print('GAME '+json.dumps(result['games'][-1]),flush=True)
    except Exception as exc:
        result['error']=repr(exc); raise
    finally:
        manager.shutdown(); result['elapsed_seconds']=time.monotonic()-start; save()


if __name__=='__main__': main()
