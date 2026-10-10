"""Live Boku coaching transport check using a legal replayed loss fixture.

This checks generation, persistence and a subsequent decision, not playing strength.
Uses an isolated ledger; refuses occupied ports and always shuts down owned processes.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import threading
import time
from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.core.session import SessionController
from app.games.boku.game import Boku
from app.inference.imajev_client import ImajevClient
from app.inference.service_manager import ServiceManager
from app.storage.atomic import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    parser.add_argument('--output', type=Path, default=Path('logs/boku-coaching-live'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = replace(load_config(args.config), game='boku', prompt_variant='coached_quoted',
                     learning_directory=args.output/'learning', directory=args.output/'sessions',
                     external_inference=False, save_sessions=True)
    app = QApplication([])
    c = SessionController(Boku(), ImajevClient(config), config)
    manager = ServiceManager(config)
    c.service_manager = manager
    c.client.manager = manager
    manager.progress = c.progress.emit
    c.changed.connect(lambda: print(c.phase+': '+c.message, flush=True))
    result = {'backend':config.coach_backend, 'fixture':'legal replayed loss; White placements are scripted',
              'memory_mib':[], 'error':None}
    stop = threading.Event()
    def monitor():
        while not stop.is_set():
            try:
                value = subprocess.check_output(['nvidia-smi','--query-gpu=memory.used',
                    '--format=csv,noheader,nounits'], text=True, timeout=3)
                result['memory_mib'].append(int(value.splitlines()[0]))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            stop.wait(1)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    def settle():
        deadline = time.monotonic()+1200
        while c.busy:
            app.processEvents()
            time.sleep(.01)
            if time.monotonic() > deadline:
                raise RuntimeError('Validation deadline exceeded')
        app.processEvents()
        if c.phase == 'error' or c.coaching_failed:
            raise RuntimeError(c.message)
    try:
        c.start(); settle()
        # Deliberately weak White moves provide known accepted evidence to the coach.
        for cell in ('A1','K1','A2','K2','A3','K3','A4','K4','A5'):
            player = c.game.current_player(c.state)
            action = 'place_'+cell
            c.state = c.game.apply_action(c.state, action)
            c.events.append({'purpose':'replayed_fixture','player':player,'accepted_action':action})
        started = time.monotonic()
        c._after_move(); settle()
        result['coaching_seconds'] = time.monotonic()-started
        result['coaching'] = json.loads(c.coach_diagnostics)
        if c.next_strategy['revision'] != 1:
            raise RuntimeError('No strategy revision committed')
        result['strategy'] = c.next_strategy
        c.new_game()
        c.state = c.game.apply_action(c.state, 'place_F5')
        c._after_move(); settle()
        decisions = [e for e in c.events if e.get('ticket',{}).get('purpose') == 'decision']
        if not decisions or any(e['request']['state']['strategy'] != c.strategy for e in decisions):
            raise RuntimeError('Revised strategy was not injected')
        if c.state.revision < 2 or c.phase != 'human':
            raise RuntimeError('Imajev did not complete a playable White turn')
        result['subsequent_decision'] = decisions[-1]
        result['subsequent_decision_verified'] = True
        print('PASS: Boku strategy revised, retained and used by an Imajev decision.', flush=True)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        while c.busy:
            app.processEvents()
            time.sleep(.01)
        manager.shutdown()
        stop.set(); thread.join(timeout=4)
        result['peak_device_mib'] = max(result['memory_mib'], default=None)
        atomic_json(args.output/'results.json', result)


if __name__ == '__main__':
    main()
