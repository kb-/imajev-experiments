"""Probe quoted Boku tactical facts using an explicitly running external service.

Does not change the GUI game or start/stop any model. A passing fixture is not
a measurement of general Boku playing strength.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import argparse
from dataclasses import replace
from pathlib import Path
import time

from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.games.boku.game import Boku
from app.inference.imajev_client import ImajevClient, InferenceError
from app.storage.atomic import atomic_json
from app.ui.rendering import observation_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    parser.add_argument('--output', type=Path, default=Path('logs/boku-tactical-facts.json'))
    args = parser.parse_args()
    app = QApplication([])
    game = Boku()
    config = replace(load_config(args.config), external_inference=True)
    client = ImajevClient(config)
    fixtures = [('logged_loss', ('E3','D2','E2','D1','E1','D3','D4','D5','E4'), 'place_E5'),
                ('opening_max_candidates', ('E3',), None),
                ('mandatory_capture', ('A2','A1','A3','K1','K2','A4'), None)]
    result = {'rows': [], 'error': None}
    try:
        for name, sequence, expected in fixtures:
            state = game.initial_state()
            for cell in sequence:
                state = game.apply_action(state, 'place_'+cell)
            started = time.monotonic()
            request = game.decision_request(state, game.legal_actions(state), prompt_variant='quoted')
            facts_seconds = time.monotonic()-started
            deadline = time.monotonic()+180
            while True:
                try:
                    reply = client.decide(request, observation_png(game.render(state, (), 'decision')))
                    break
                except InferenceError as exc:
                    if 'previous GPU request' not in str(exc) or time.monotonic() > deadline:
                        raise
                    time.sleep(1)
            action = game.decode_decision(state, reply)
            row = {'fixture': name, 'request': request, 'reply': reply.raw,
                   'facts_seconds': facts_seconds, 'seconds': time.monotonic()-started,
                   'selected': action, 'expected': expected,
                   'passed': expected is None or action == expected}
            result['rows'].append(row)
            atomic_json(args.output, result)
            print(f'{name}: {action}; expected={expected}; passed={row["passed"]}; '
                  f'facts={facts_seconds:.3f}s; total={row["seconds"]:.2f}s', flush=True)
        if not all(row['passed'] for row in result['rows']):
            raise RuntimeError('Model missed a tactical fixture despite supplied facts.')
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        atomic_json(args.output, result)
    return app


if __name__ == '__main__':
    main()
