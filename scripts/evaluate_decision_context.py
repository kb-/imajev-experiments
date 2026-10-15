"""Replay the archived full-context Tic-tac-toe benchmark against a text opponent.

The model receives definitions, rules, general tactics, board and history. All
minimax scores stay in the evaluator; no tactical corrections enter decisions.
"""
import argparse
import copy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import time

from app.config import load_config
from app.games.tic_tac_toe.game import TicTacToe
from app.games.tic_tac_toe.prompting import winning_cells
from app.inference.opponents import create_opponent
from app.inference.service_manager import ServiceManager
from scripts.evaluate import oracle
from scripts.inference_setup import active_inference, identity, write_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / 'docs/evaluation/decision2-nox-q8-results.json'


def archived_request(baseline, case):
    """Reconstruct the exact input independently of runtime/model identity."""
    request = copy.deepcopy(baseline['request_template'])
    request['state'].update(baseline['request_positions'][case['request_position']])
    request['questions']['move']['criteria'] = {
        action: baseline['choice_description_template'].format(cell=action.removeprefix('place_'))
        for action in case['option_order']}
    return request


def game_request(baseline, state, actions):
    request = copy.deepcopy(baseline['request_template'])
    request['state'].update(starting_player=state.starting_player, player_to_move=state.next_player,
        board=[[mark or '.' for mark in state.board[i:i+3]] for i in (0, 3, 6)],
        move_history=[{'player': move.player, 'cell': move.action.removeprefix('place_')} for move in state.history])
    request['questions']['move']['criteria'] = {
        action.id: baseline['choice_description_template'].format(cell=action.id.removeprefix('place_'))
        for action in actions}
    return request


def scores_for(game, state):
    return {action.id: oracle((after := game.apply_action(state, action.id)).board, after.next_player)
            for action in game.legal_actions(state)}


def summary(cases, games):
    canonical = [c for c in cases if c['order'] == 'canonical']
    nonlosing = [c for c in cases if max(c['oracle_scores'].values()) >= 0]
    return {'unique_positions': len({c['position'] for c in cases}), 'position_requests': len(cases),
            'canonical_optimal': sum(c['optimal'] for c in canonical), 'canonical_total': len(canonical),
            'optimal_choices': sum(c['optimal'] for c in cases),
            'avoidable_losses': sum(c['avoidable_loss'] for c in cases), 'nonlosing_requests': len(nonlosing),
            'missed_immediate_wins': sum(c['missed_immediate_win'] for c in cases),
            'immediate_win_opportunities': sum(c['immediate_win_opportunity'] for c in cases),
            'failed_immediate_defenses': sum(c['failed_immediate_defense'] for c in cases),
            'positions_with_different_choices_by_order': sum(
                len({c['action'] for c in cases if c['position'] == index}) > 1
                for index in {c['position'] for c in cases}),
            'full_games': len(games), 'draws': sum(g['kind'] == 'draw' for g in games),
            'O_wins': sum(g['winner'] == 'O' for g in games), 'O_losses': sum(g['winner'] == 'X' for g in games)}


def evaluate(config_path, baseline_path, output_path, limit=None, games_per_starter=6):
    active = active_inference(ROOT)
    if active:
        raise RuntimeError(f'Close the application/inference first (PIDs: {active}).')
    baseline = json.loads(baseline_path.read_text())
    if baseline.get('version') != 1 or not baseline.get('cases') or not baseline.get('request_template'):
        raise ValueError('Expected a version-1 full-context benchmark archive.')
    config = replace(load_config(config_path), tactical_guard=False, move_temperature=0.)
    if config.opponent_settings.input_mode != 'text':
        raise ValueError('This benchmark requires a text opponent profile.')
    manager = ServiceManager(config)
    backend = create_opponent(config, manager=manager)
    game = TicTacToe()
    report = {'version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
              'method': 'Exact archived full-context requests, no guard or sampling; full games use identical definitions and general tactics.',
              'baseline': identity(baseline_path), 'opponent': asdict(config.opponent_settings),
              'limit': limit, 'games_per_starter': games_per_starter,
              'cases': [], 'games': [], 'experiment_completed': False}
    selected_positions = sorted({c['position'] for c in baseline['cases']})
    if limit is not None:
        selected_positions = selected_positions[:limit]
    selected = set(selected_positions)
    def decide(state, request):
        before = time.monotonic()
        result = backend.choose_move(request)
        action = game.decode_decision(state, result)
        return action, {'request': request, 'response': result.raw,
                        'seconds': time.monotonic() - before, 'action': action}
    try:
        for old in baseline['cases']:
            if old['position'] not in selected:
                continue
            request = archived_request(baseline, old)
            # Rebuild the state from history to validate the board and legal set.
            context = request['state']
            state = game.initial_state_for_player(context['starting_player'])
            for move in context['move_history']:
                if state.next_player != move['player']:
                    raise ValueError('Archived history has an inconsistent player.')
                state = game.apply_action(state, 'place_' + move['cell'])
            if ([[v or '.' for v in state.board[i:i+3]] for i in (0, 3, 6)] != context['board']
                    or state.next_player != 'O'
                    or set(request['questions']['move']['criteria']) != {a.id for a in game.legal_actions(state)}):
                raise ValueError('Archived request does not match a legal O-turn position.')
            scores = scores_for(game, state)
            if scores != old['oracle_scores']:
                raise ValueError('Archived oracle scores differ from the rules engine.')
            action, row = decide(state, request)
            wins = winning_cells(state.board, 'O')
            row.update(position=old['position'], revision=state.revision, order=old['order'], oracle_scores=scores,
                       optimal=scores[action] == max(scores.values()),
                       avoidable_loss=scores[action] < 0 <= max(scores.values()),
                       immediate_win_opportunity=bool(wins),
                       missed_immediate_win=bool(wins) and game.outcome(game.apply_action(state, action)).winner != 'O',
                       failed_immediate_defense=not wins and bool(winning_cells(state.board, 'X')) and scores[action] < max(scores.values()))
            report['cases'].append(row)
            if len(report['cases']) % 30 == 0:
                write_json(output_path, report)
                print(f'Positions completed: {len(report["cases"]) // 3}/{len(selected_positions)}', flush=True)
        for starter in ('X', 'O'):
            for seed in range(games_per_starter):
                state = game.initial_state_for_player(starter)
                rng = random.Random(seed)
                turns = []
                while game.outcome(state).kind == 'ongoing':
                    actions = game.legal_actions(state)
                    if len(actions) == 1:
                        action = actions[0].id
                        turns.append({'player': state.next_player, 'action': action, 'forced_single_legal_action': True})
                    elif state.next_player == 'O':
                        action, row = decide(state, game_request(baseline, state, actions))
                        turns.append(dict(row, player='O'))
                    else:
                        scores = scores_for(game, state)
                        action = rng.choice([a for a, score in scores.items() if score == min(scores.values())])
                        turns.append({'player': 'X', 'action': action})
                    state = game.apply_action(state, action)
                outcome = game.outcome(state)
                report['games'].append({'starter': starter, 'seed': seed, 'kind': outcome.kind,
                                        'winner': outcome.winner, 'turns': turns})
                print(f'Full game starter={starter} seed={seed}: {outcome.kind}, winner={outcome.winner}', flush=True)
        report['summary'] = summary(report['cases'], report['games'])
        report['experiment_completed'] = True
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        try:
            manager.shutdown()
            report['cleanup_confirmed'] = True
        except BaseException as exc:
            report['experiment_completed'] = False
            report['cleanup_error'] = str(exc)
            raise
        finally:
            write_json(output_path, report)
    print(json.dumps(report['summary'], indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/decision3-mini-4b-q8.yaml')
    parser.add_argument('--baseline', type=Path, default=DEFAULT_BASELINE)
    parser.add_argument('--output', type=Path, default=ROOT / 'logs/decision-context-benchmark.json')
    parser.add_argument('--limit', type=int, help='Pilot: maximum distinct positions; every legal choice is retained.')
    parser.add_argument('--games-per-starter', type=int, default=6, help='Seeds per starter; zero skips full games.')
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or args.games_per_starter < 0:
        parser.error('--limit must be positive and --games-per-starter non-negative.')
    evaluate(args.config, args.baseline, args.output, args.limit, args.games_per_starter)


if __name__ == '__main__':
    main()
