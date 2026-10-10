"""Replay the saved v5 refusal and Retry prompts against a running service.

Applies no moves and starts/stops no processes; the GUI board is unchanged.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.games.boku.game import Boku
from app.inference.imajev_client import InferenceError
from app.inference.opponents import create_opponent, role_metadata, named_result
from app.storage.atomic import atomic_json
from app.ui.rendering import observation_png


SOURCE = Path('docs/evaluation/fixtures/boku/abstained-loss-session.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    parser.add_argument('--output', type=Path, default=Path('logs/boku-refusal-retries.json'))
    args = parser.parse_args()
    app = QApplication([])
    game = Boku()
    record = json.loads(SOURCE.read_text())
    event = next(e for e in record['events'] if e['ticket']['state_revision'] == 32)
    state = game.decode_state(event['state'])
    actions = game.legal_actions(state)
    request = game.decision_request(state, actions, prompt_variant='quoted')
    if json.loads(json.dumps(request)) != event['request']:
        raise ValueError('Current prompt differs from the saved v5 refusal; refusing a misleading replay.')
    config = replace(load_config(args.config), external_inference=True)
    client = create_opponent(config)
    png = observation_png(game.render(state, (), 'decision'))
    result = {'inference_protocol': 2, 'inference': role_metadata(config), 'source': str(SOURCE), 'prompt_version': game.prompt_version,
              'rows': [], 'error': None}
    try:
        for attempt in (0, 1, 2):
            request = (event['request'] if attempt == 0 else game.retry_decision_request(
                state, actions, attempt, prompt_variant='quoted'))
            started = time.monotonic()
            deadline = started + 180
            while True:
                try:
                    reply = client.choose_move(request, png)
                    break
                except InferenceError as exc:
                    if 'previous GPU request' not in str(exc) or time.monotonic() > deadline:
                        raise
                    time.sleep(1)
            answer = reply
            row = {'attempt': attempt, 'request': request, 'reply': reply.raw,
                   'choice': answer.choice, 'abstained': answer.abstained,
                   'unknown_probability': answer.unknown_probability,
                   'seconds': time.monotonic() - started}
            try:
                row['accepted_action'] = game.decode_decision(state, reply)
            except ValueError as exc:
                row['rejection'] = str(exc)
            result['rows'].append(row)
            atomic_json(args.output, result)
            print(json.dumps({k: v for k, v in row.items() if k not in ('request', 'reply')}), flush=True)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        atomic_json(args.output, result)
    return app


if __name__ == '__main__':
    main()
