"""Live NF4 acceptance: two synthetic losses, then a game with real O decisions.

Uses installed assets only. X moves are symbolic engine actions, not handwriting
recognition. Owns and shuts down the inference service; refuses an occupied port.
"""
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
from app.games.tic_tac_toe.game import TicTacToe
from app.inference.imajev_client import ImajevClient
from app.inference.service_manager import ServiceManager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.yaml'))
    parser.add_argument('--directory', type=Path, default=Path('logs/learning-validation'))
    args = parser.parse_args()
    application = QApplication([])
    config = replace(load_config(args.config), learning_enabled=True,
                     learning_directory=args.directory, external_inference=False)
    controller = SessionController(TicTacToe(), ImajevClient(config), config)
    manager = ServiceManager(config)
    controller.service_manager = manager
    controller.client.manager = manager
    samples = []
    stop = threading.Event()

    def monitor():
        while not stop.wait(.2):
            result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                samples.append(int(result.stdout.splitlines()[0]))

    thread = threading.Thread(target=monitor)
    thread.start()

    def wait():
        deadline = time.monotonic() + 360
        while controller.busy:
            application.processEvents()
            if time.monotonic() > deadline:
                raise RuntimeError('Live worker exceeded verification timeout.')
            time.sleep(.02)
        application.processEvents()

    def check_coach():
        if controller.coaching_failed:
            raise RuntimeError(controller.message + '\n' + controller.coach_diagnostics)

    metrics = {'synthetic_losses': 2, 'human_moves': 'symbolic engine actions', 'coaching': []}
    try:
        controller.start()
        wait()
        if not controller.ready:
            raise RuntimeError(controller.message)
        for _ in range(2):
            controller.new_game()
            for cell in ('A1', 'A2', 'B1', 'B2', 'C1'):
                controller.state = controller.game.apply_action(controller.state, 'place_' + cell)
            start = time.monotonic()
            controller._after_move()
            wait()
            check_coach()
            ledger = controller.learning_store.ledger()
            metrics['coaching'].append({'revision': ledger['revision'], 'seconds': time.monotonic() - start,
                                        'strategy': ledger['text'],
                                        'included_game_ids': ledger['revisions'][-1]['request']['included_game_ids']})
            print(json.dumps(metrics['coaching'][-1], indent=2), flush=True)
        controller.new_game(alternate_starter=True)
        wait()
        assert controller.strategy_revision == ledger['revision']
        retries = 0
        while controller.game.outcome(controller.state).kind == 'ongoing':
            if controller.phase == 'error':
                if retries >= 5:
                    raise RuntimeError(controller.message)
                retries += 1
                controller.retry()
            elif controller.game.current_player(controller.state) == 'X':
                action = controller.game.legal_actions(controller.state)[0].id
                controller.state = controller.game.apply_action(controller.state, action)
                controller._after_move()
            else:
                controller._after_move()
            wait()
        check_coach()
        metrics.update(outcome=controller.game.outcome(controller.state).kind,
                       winner=controller.game.outcome(controller.state).winner,
                       moves=len(controller.state.history), retries=retries,
                       peak_sampled_vram_mib=max(samples) if samples else None)
        args.directory.mkdir(parents=True, exist_ok=True)
        (args.directory / 'metrics.json').write_text(json.dumps(metrics, indent=2))
        print(json.dumps(metrics, indent=2), flush=True)
    finally:
        stop.set()
        thread.join()
        manager.shutdown()


if __name__ == '__main__':
    main()
