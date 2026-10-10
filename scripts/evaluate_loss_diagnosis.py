"""Re-review a completed loss without modifying its strategy ledger."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from app.config import load_config
from app.games.tic_tac_toe.coaching import loss_context
from app.inference.coach import diagnose_then_coach
from app.inference.service_manager import ServiceManager
from app.storage.coached import CoachedStore, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-id', required=True)
    parser.add_argument('--history', type=Path, default=Path('learning/coached-quoted'))
    parser.add_argument('--model', default='qwen3.6:latest')
    parser.add_argument('--threat-facts', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = CoachedStore(args.history).ledger()
    # Reproduce the original history window and prior rules, including consumed games.
    revision = next(r for r in ledger['revisions'] if r['trigger_game_id'] == args.game_id)
    request = loss_context(revision['request'], include_threats=args.threat_facts)
    config = replace(load_config(Path('config.coached-quoted.ollama.yaml')), coach_model=args.model)
    manager = ServiceManager(config)
    manager.progress = lambda message: print(message, flush=True)
    result = {'request': request, 'stages': [], 'response': None, 'error': None}
    start = time.monotonic()

    def save():
        atomic_json(args.output / 'results.json', result)

    def record(stage):
        result['stages'].append(stage)
        save()

    try:
        result['response'] = manager.ollama(lambda invoke: diagnose_then_coach(
            request, invoke, record, 7500,
            on_diagnosis=lambda response: print('DIAGNOSIS\n' + response['summary'], flush=True)))
        print('STRATEGY\n' + json.dumps(result['response']['strategy'], ensure_ascii=False), flush=True)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        manager.shutdown()
        result['seconds'] = time.monotonic() - start
        save()


if __name__ == '__main__':
    main()
