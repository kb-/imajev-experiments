"""Live coached-quoted lifecycle check with scripted legal X moves.

The solver controls the test's human opponent only, never Imajev or its coach.
Uses an isolated local ledger and always shuts down its owned service.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import threading
import time
from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.imajev_client import ImajevClient
from app.inference.service_manager import ServiceManager
from scripts.evaluate import oracle


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('config.coached-quoted.yaml'))
    parser.add_argument('--output',type=Path,default=Path('logs')/('coached-quoted-live-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')))
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    config=replace(load_config(args.config),prompt_variant='coached_quoted',learning_directory=args.output/'learning',external_inference=False)
    app=QApplication([])
    c=SessionController(TicTacToe(),ImajevClient(config),config)
    manager=ServiceManager(config); c.service_manager=manager; c.client.manager=manager; manager.progress=c.progress.emit
    results={'games':[],'memory_mib':[],'backend':config.coach_backend}
    stop=threading.Event()
    def memory():
        while not stop.is_set():
            try:
                value=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],text=True,timeout=3)
                results['memory_mib'].append(int(value.splitlines()[0]))
            except (OSError,ValueError,subprocess.SubprocessError): pass
            stop.wait(1)
    monitor=threading.Thread(target=memory,daemon=True); monitor.start()
    def settle():
        deadline=time.monotonic()+900
        while c.busy:
            app.processEvents(); time.sleep(.01)
            if time.monotonic()>deadline: raise RuntimeError('Background operation exceeded validation deadline')
        app.processEvents()
        if c.coaching_failed or c.phase=='error': raise RuntimeError(c.message)
    order=['place_A1','place_C3','place_C1','place_A3','place_B2','place_B1','place_A2','place_C2','place_B3']
    def play(index):
        if index: c.new_game(alternate_starter=True); settle()
        initial_revision=c.strategy_revision
        while c.game.outcome(c.state).kind=='ongoing':
            assert c.game.current_player(c.state)=='X'
            actions=c.game.legal_actions(c.state)
            scores={a.id:oracle((after:=c.game.apply_action(c.state,a.id)).board,after.next_player) for a in actions}
            tie_order=order[index % len(order):]+order[:index % len(order)]
            action=next(a for a in tie_order if scores.get(a)==min(scores.values()))
            c.state=c.game.apply_action(c.state,action)
            c.events.append({'player':'X','accepted_action':action,'purpose':'scripted_human'})
            c._after_move(); settle()
        outcome=c.game.outcome(c.state)
        row={'game_id':c.session_id,'outcome':outcome.kind,'winner':outcome.winner,
             'initial_strategy_revision':initial_revision,'next_strategy_revision':c.next_strategy['revision']}
        results['games'].append(row)
        print(json.dumps(row),flush=True)
    try:
        c.start(); settle()
        for i in range(6):
            play(i)
            if c.next_strategy['revision']:
                break
        else: raise RuntimeError('No loss occurred in six games; loss-driven live update remains unvalidated.')
        learned=c.next_strategy['strategy']
        play(i+1)
        decisions=[e for e in c.events if e.get('ticket',{}).get('purpose')=='decision']
        assert decisions and all(e['request']['state']['strategy']==learned for e in decisions)
        results['strategy']=c.learning_store.ledger()
        results['subsequent_game_injection_verified']=True
        print('PASS strategy update retained and injected in subsequent playable game',flush=True)
    except Exception as exc:
        results['error']=repr(exc)
        raise
    finally:
        # Never destroy a QThreadPool while its worker still owns the GPU.
        while c.busy:
            app.processEvents(); time.sleep(.01)
        manager.shutdown(); stop.set(); monitor.join(timeout=4)
        results['peak_device_mib']=max(results['memory_mib'],default=None)
        (args.output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
