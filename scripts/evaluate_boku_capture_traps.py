"""Experimental bounded capture-trap evidence; does not change app prompting.

For each White candidate, check whether Black can place and capture, after
which every complete White reply still allows a winning Black turn. This is
limited to capture setups, not an exhaustive search of all two-turn threats.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import time

from app.games.boku.game import Boku
from app.games.boku.tactics import black_winning_replies
from app.games.boku.capture_traps import compact, capture_trap, evaluate
from app.storage.atomic import atomic_json


def probe_model(game, state, result, config_path):
    """Compare facts alone against facts plus a general strategy priority."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication
    from app.config import load_config
    from app.inference.imajev_client import ImajevClient, InferenceError
    from app.ui.rendering import observation_png
    app = QApplication.instance() or QApplication([])
    config = replace(load_config(config_path), external_inference=True)
    client = ImajevClient(config)
    request = game.decision_request(state, game.legal_actions(state), prompt_variant='quoted')
    flag = 'allows_Black_capture_forced_win'
    request['state']['candidate_fact_defaults'][flag] = False
    request['state']['candidate_fact_scope'] += (
        ' allows_Black_capture_forced_win means that after this White action, '
        'Black can place and capture so that every complete White reply still '
        'allows Black to win on its following turn. This only tests capture setups. '
        'White may choose any eligible mandatory capture to avoid the trap.')
    for row in result['rows']:
        if row[flag]:
            request['questions']['move']['criteria'][row['action']][flag] = True
    if set(request['questions']['move']['criteria']) != {r['action'] for r in result['rows']}:
        raise ValueError('Live probes require facts for every legal candidate.')
    result['probes'] = []
    for arm in ('facts_only', 'facts_and_priority'):
        if arm == 'facts_and_priority':
            request['state']['strategy'].append('otherwise avoid allowing Black to force a win through a capture')
        then = time.monotonic()
        deadline = then+180
        while True:
            try:
                reply = client.decide(request, observation_png(game.render(state, (), 'decision')))
                break
            except InferenceError as exc:
                if 'previous GPU request' not in str(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(1)
        action = game.decode_decision(state, reply)
        # Snapshot the mutable request before the next arm adds a strategy rule.
        row = {'arm': arm, 'request': json.loads(json.dumps(request)), 'reply': reply.raw,
               'action': action, 'avoids_checked_capture_traps': not next(r[flag] for r in result['rows'] if r['action']==action),
               'seconds': time.monotonic()-then}
        result['probes'].append(row)
        print(json.dumps({k:row[k] for k in ('arm','action','avoids_checked_capture_traps','seconds')}), flush=True)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, default=Path('sessions/015e9e8c-b396-44c2-b431-fc059e97bb85/session.json'))
    parser.add_argument('--revision', type=int, default=25)
    parser.add_argument('--actions', nargs='*')
    parser.add_argument('--output', type=Path, default=Path('logs/boku-capture-traps.json'))
    parser.add_argument('--probe-model', action='store_true', help='Test the evidence on the already running external service')
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    args = parser.parse_args()
    record = json.loads(args.session.read_text())
    game = Boku()
    final = game.decode_state(record['state'])
    state = game.initial_state()
    for move in final.history:
        if state.revision >= args.revision:
            break
        state = game.apply_action(state, move.action)
    if state.revision != args.revision or state.next_player != 'White':
        raise ValueError('Select an accepted White-to-move revision.')
    result = {'session': str(args.session), 'revision': args.revision,
              'state': game.encode_state(state), 'rows': [], 'error': None}
    started = time.monotonic()
    try:
        for action in game.legal_actions(state):
            if args.actions and action.id not in args.actions:
                continue
            then = time.monotonic()
            row = {'action': action.id, **evaluate(game, state, action.id), 'seconds': time.monotonic()-then}
            result['rows'].append(row)
            atomic_json(args.output, result)
            print(json.dumps(row), flush=True)
        result['facts_seconds'] = time.monotonic()-started
        if args.probe_model:
            probe_model(game, state, result, args.config)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        result['seconds'] = time.monotonic()-started
        result['reply_cache'] = black_winning_replies.cache_info()._asdict()
        atomic_json(args.output, result)


if __name__ == '__main__':
    main()
