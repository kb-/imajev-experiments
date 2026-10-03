"""Live contract, handwriting and opponent evaluation. Requires the local model.

The minimax oracle is used only here; it is never an opponent fallback.
"""
import argparse
from dataclasses import replace
from functools import lru_cache
import json
import os
from pathlib import Path
import statistics
import time
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.core.contracts import Stroke
from app.games.tic_tac_toe.game import State, TicTacToe
from app.inference.imajev_client import ImajevClient, InferenceError
from app.ui.rendering import observation_png

game = TicTacToe()


@lru_cache(None)
def oracle(board, player):
    state = State(board=board, next_player=player)
    outcome = game.outcome(state)
    if outcome.kind == 'win':
        return 1 if outcome.winner == 'O' else -1
    if outcome.kind == 'draw':
        return 0
    scores = [oracle(after.board, after.next_player) for action in game.legal_actions(state)
              for after in (game.apply_action(state, action.id),)]
    return max(scores) if player == 'O' else min(scores)


def o_turn_states():
    seen = set()
    def visit(state):
        if state.board in seen:
            return
        seen.add(state.board)
        if game.outcome(state).kind != 'ongoing':
            return
        if state.next_player == 'O':
            yield state
        for action in game.legal_actions(state):
            yield from visit(game.apply_action(state, action.id))
    return visit(State())


def timings(values):
    if not values:
        return {'p50_seconds': None, 'p95_seconds': None}
    ordered = sorted(values)
    return {'p50_seconds': statistics.median(values), 'p95_seconds': ordered[min(len(ordered)-1, int(.95 * len(ordered)))]}


def recognition(corpus, split, client, config):
    raw = json.loads(corpus.read_text())
    if raw.get('version') != 1 or not isinstance(raw.get('samples'), list):
        raise ValueError('Expected a version-1 corpus with samples.')
    samples = [sample for sample in raw['samples'] if sample['split'] == split]
    results, durations = [], []
    for sample in samples:
        drawing = tuple(Stroke(tuple(tuple(p) for p in s['points']), s.get('width', .012), s.get('color', '#297a70')) for s in sample['strokes'])
        # decode_state validates drawing shape/coordinates; geometry remains a location gate.
        validation = game.encode_state(game.apply_action(State(), 'place_A1', drawing))
        game.decode_state(validation)
        expected = f'place_{sample["cell"]}' if sample['valid_x'] else None
        accepted, error, reply = None, None, None
        start = time.monotonic()
        try:
            reply = client.decide(game.recognition_request(State(), drawing), observation_png(game.render(State(), drawing, 'recognition'), config.observation_size))
            accepted = game.decode_recognition(State(), drawing, reply, config.threshold)
        except (ValueError, InferenceError) as exc:
            error = str(exc)
        durations.append(time.monotonic() - start)
        results.append({'id': sample['id'], 'participant': sample['participant'], 'expected': expected,
                        'accepted': accepted, 'error': error, 'reply': dict(reply.raw) if reply else None})
        print(f'{sample["id"]}: {accepted or error}', flush=True)
    accepted = [r for r in results if r['accepted']]
    valid = [r for r in results if r['expected']]
    invalid = [r for r in results if r['expected'] is None]
    correct = sum(r['accepted'] == r['expected'] for r in accepted)
    participants = {r['participant'] for r in results}
    sufficient = len(raw['samples']) >= 200 and len(participants) >= 2 and split == 'heldout' and any(s['split'] == 'pilot' for s in raw['samples'])
    return {'mode': 'recognition', 'split': split, 'sample_count': len(results), 'participants': len(participants),
            'accepted_move_precision': correct / len(accepted) if accepted else None,
            'valid_x_acceptance_coverage': sum(r['accepted'] == r['expected'] for r in valid) / len(valid) if valid else None,
            'invalid_false_acceptance_rate': sum(bool(r['accepted']) for r in invalid) / len(invalid) if invalid else None,
            'sufficient_for_acceptance_review': sufficient, **timings(durations), 'results': results}


def opponent(client, config, limit, tactical_guard=False):
    results, durations = [], []
    for state in o_turn_states():
        if limit and len(results) >= limit:
            break
        actions = game.legal_actions(state)
        scores = {action.id: oracle(after.board, after.next_player) for action in actions for after in (game.apply_action(state, action.id),)}
        optimal = {key for key, score in scores.items() if score == max(scores.values())}
        wins = {action.id for action in actions if game.outcome(game.apply_action(state, action.id)).winner == 'O'}
        threats = {action.id for action in actions if game.outcome(game.apply_action(replace(state, next_player='X'), action.id)).winner == 'X'}
        blocks = set()
        for action in actions:
            after = game.apply_action(state, action.id)
            if all(game.outcome(game.apply_action(after, candidate.id)).winner != 'X' for candidate in game.legal_actions(after)):
                blocks.add(action.id)
        error, selected, proposed, correction, reply = None, None, None, None, None
        start = time.monotonic()
        try:
            if len(actions) == 1:
                selected = actions[0].id
            else:
                reply = client.decide(game.decision_request(state, actions), observation_png(game.render(state, (), 'decision'), config.observation_size))
                selected = game.decode_decision(state, reply)
            proposed = selected
            if tactical_guard:
                selected, correction = game.tactical_choice(state, proposed)
            game.apply_action(state, selected)
        except (ValueError, InferenceError) as exc:
            error = str(exc)
        durations.append(time.monotonic() - start)
        results.append({'state': game.encode_state(state), 'model_proposed': proposed, 'selected': selected, 'tactical_correction': correction, 'optimal': sorted(optimal),
                        'agreement': selected in optimal, 'missed_win': bool(wins) and selected not in wins,
                        'missed_necessary_block': bool(threats and blocks and not wins) and selected not in blocks,
                        'error': error, 'reply': dict(reply.raw) if reply else None})
        print(f'{len(results)}: {selected or error}', flush=True)
    completed = [r for r in results if not r['error']]
    return {'mode': 'opponent', 'tactical_guard': tactical_guard, 'states': len(results), 'failed_or_abstained': len(results)-len(completed),
            'tactical_corrections': sum(bool(r['tactical_correction']) for r in completed),
            'raw_optimal_action_agreement': sum(r['model_proposed'] in r['optimal'] for r in completed)/len(completed) if completed else None,
            'optimal_action_agreement': sum(r['agreement'] for r in completed)/len(completed) if completed else None,
            'missed_wins': sum(r['missed_win'] for r in completed),
            'missed_necessary_blocks': sum(r['missed_necessary_block'] for r in completed),
            'illegal_committed_actions': 0, **timings(durations), 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.yaml'))
    parser.add_argument('--output', type=Path, default=Path('evaluation.json'))
    commands = parser.add_subparsers(dest='mode', required=True)
    commands.add_parser('contract')
    recognize = commands.add_parser('recognition')
    recognize.add_argument('corpus', type=Path)
    recognize.add_argument('--split', choices=('pilot', 'heldout'), default='heldout')
    choose = commands.add_parser('opponent')
    choose.add_argument('--limit', type=int, default=0, help='0 evaluates every reachable ongoing O-turn state')
    choose.add_argument('--with-tactical-guard', action='store_true', help='Measure the assisted gameplay policy as well as raw model choices')
    args = parser.parse_args()
    app = QApplication([])
    config = load_config(args.config)
    client = ImajevClient(config)
    started = time.monotonic()
    try:
        warmup = client.warmup(game.recognition_request(State(), ()), observation_png(game.render(State(), (), 'recognition'), config.observation_size))
        if args.mode == 'recognition':
            report = recognition(args.corpus, args.split, client, config)
        elif args.mode == 'opponent':
            report = opponent(client, config, args.limit, args.with_tactical_guard)
        else:
            state = game.apply_action(State(), 'place_A1')
            reply = client.decide(game.decision_request(state, game.legal_actions(state)), observation_png(game.render(state, (), 'decision'), config.observation_size))
            game.apply_action(state, game.decode_decision(state, reply))
            report = {'mode': 'contract', 'passed': True, 'recognition_reply': dict(warmup.raw), 'decision_reply': dict(reply.raw)}
    except (InferenceError, ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f'Evaluation failed: {exc}\n')
    report.update(model=config.expected_model, threshold=config.threshold, total_seconds=time.monotonic()-started,
                  warmup_reply=dict(warmup.raw), hardware_metrics='Measure peak VRAM/RAM externally on the target service host.')
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    print(f'Saved report: {args.output}')


if __name__ == '__main__':
    main()
