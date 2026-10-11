"""Measure recognition controls and prove shared-runtime reuse on the local GPU."""
import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

from app.config import load_config
from app.core.contracts import Stroke
from app.games.boku.game import Boku
from app.games.tic_tac_toe.game import TicTacToe, CELLS, center
from app.inference.opponents import create_opponent, role_metadata
from app.inference.recognition import create_recognizer, metadata
from app.inference.service_manager import ServiceManager
from app.storage.atomic import atomic_json
from app.ui.rendering import observation_png
from scripts.validate_boku import drawing as boku_drawing


def cross(cell):
    x, y = center(CELLS.index(cell))
    return (Stroke(((x-.09, y-.09), (x+.09, y+.09))),
            Stroke(((x+.09, y-.09), (x-.09, y+.09))))


def cases():
    ttt, boku = TicTacToe(), Boku()
    for cell in ('A1', 'B2', 'C3'):
        yield f'ttt-X-{cell}', ttt, ttt.initial_state(), cross(cell), f'place_{cell}'
    yield 'ttt-blank', ttt, ttt.initial_state(), (), None
    yield 'ttt-dot', ttt, ttt.initial_state(), (Stroke(((.15,.15),(.151,.151))),), None
    yield 'ttt-multiple', ttt, ttt.initial_state(), cross('A1') + cross('B1'), None
    yield 'ttt-wrong-symbol', ttt, ttt.initial_state(), (
        Stroke(tuple((.17+.08*math.cos(i*math.tau/40), .17+.08*math.sin(i*math.tau/40))
                     for i in range(41))),), None
    occupied = ttt.apply_action(ttt.initial_state(), 'place_A1')
    occupied = ttt.apply_action(occupied, 'place_B2')
    yield 'ttt-occupied', ttt, occupied, cross('A1'), None
    for cell in ('F5', 'A1', 'K5'):
        yield f'boku-O-{cell}', boku, boku.initial_state(), boku_drawing(cell,'O'), f'place_{cell}'
    yield 'boku-wrong-symbol', boku, boku.initial_state(), boku_drawing('F5','X'), None
    yield 'boku-multiple', boku, boku.initial_state(), boku_drawing('F5','O') + boku_drawing('F6','O'), None
    capture = boku.initial_state()
    for cell in ('A1','A2','K1','A3','A4'):
        capture = boku.apply_action(capture, 'place_' + cell)
    yield 'boku-capture', boku, capture, boku_drawing('A2','X'), 'capture_A2'
    yield 'boku-capture-wrong-symbol', boku, capture, boku_drawing('A2','O'), None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('configs/models/liquid-d1-3b-shared.yaml'))
    parser.add_argument('--output', type=Path, default=Path('logs/liquid-recognition-validation.json'))
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    config = load_config(args.config)
    manager = ServiceManager(config)
    manager.progress = lambda message: print(message, flush=True)
    recognizer = create_recognizer(config, manager=manager)
    opponent = create_opponent(config, recognizer, manager)
    report = {'version': 1, 'inference': role_metadata(config), 'threshold': config.threshold,
              'cases': [], 'cycles': [], 'memory_mib': [], 'error': None}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    def memory():
        while not stop.is_set():
            try:
                value = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                                                text=True, timeout=3).splitlines()[0]
                report['memory_mib'].append(int(value))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            stop.wait(.25)

    monitor = threading.Thread(target=memory, daemon=True)
    monitor.start()
    try:
        game = TicTacToe()
        blank = game.initial_state()
        warmup = recognizer.warmup(game.recognition_request(blank, ()),
                                   observation_png(game.render(blank, (), 'recognition'), config.observation_size))
        report['warmup'] = dict(warmup.raw)
        for name, game, state, ink, expected in cases():
            request = game.recognition_request(state, ink)
            reply = recognizer.recognize(request, observation_png(game.render(state, ink, 'recognition'), config.observation_size))
            accepted = rejection = None
            try:
                accepted = game.decode_recognition(state, ink, reply, config.threshold)
            except ValueError as exc:
                rejection = str(exc)
            report['cases'].append({'id': name, 'game': game.id, 'expected': expected, 'accepted': accepted,
                                    'rejection': rejection, 'correct': accepted == expected,
                                    'state': game.encode_state(state), 'drawing': [asdict(s) for s in ink],
                                    'request': request, 'result': metadata(reply), 'reply': dict(reply.raw)})
            print(f'{name}: {accepted or rejection}', flush=True)
            atomic_json(args.output, report)
        game = TicTacToe()
        state = game.initial_state()
        for turn in range(3):
            if game.outcome(state).kind != 'ongoing': break
            cell = next(action.id.removeprefix('place_') for action in game.legal_actions(state))
            ink = cross(cell)
            reply = recognizer.recognize(game.recognition_request(state, ink),
                observation_png(game.render(state, ink, 'recognition'), config.observation_size))
            row = {'turn': turn, 'recognition': dict(reply.raw), 'accepted': None, 'error': None}
            report['cycles'].append(row)
            try:
                action = game.decode_recognition(state, ink, reply, config.threshold)
                if action != 'place_' + cell: raise ValueError('Recognition selected the wrong cell.')
                state = game.apply_action(state, action, ink)
                row['accepted'] = action
                if game.outcome(state).kind != 'ongoing': break
                result = opponent.choose_move(game.decision_request(state, game.legal_actions(state),
                    prompt_variant=config.prompt_variant, opening_suggestion=config.opening_suggestion),
                    observation_png(game.render(state, (), 'decision'), config.observation_size))
                row['opponent'] = dict(result.raw)
                action = game.decode_decision(state, result)
                state = game.apply_action(state, action)
                row['opponent_action'] = action
            except ValueError as exc:
                row['error'] = str(exc)
                break
            print(f'cycle {turn}: {row["accepted"]} → {row.get("opponent_action")}', flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        raise
    finally:
        try:
            manager.shutdown()
        finally:
            stop.set(); monitor.join(4)
            valid = [r for r in report['cases'] if r['expected']]
            invalid = [r for r in report['cases'] if r['expected'] is None]
            accepted = [r for r in report['cases'] if r['accepted']]
            responses = [report['warmup']] if 'warmup' in report else []
            responses += [r['reply'] for r in report['cases']]
            responses += [r[k] for r in report['cycles'] for k in ('recognition','opponent') if k in r]
            pids = {r.get('runtime_pid') for r in responses}
            times = [r['inference_seconds'] for r in responses[1:] if 'inference_seconds' in r]
            report['summary'] = {
                'valid_cases': len(valid), 'invalid_cases': len(invalid),
                'valid_acceptance_coverage': sum(r['correct'] for r in valid) / len(valid) if valid else None,
                'invalid_false_acceptance_rate': sum(bool(r['accepted']) for r in invalid) / len(invalid) if invalid else None,
                'accepted_precision': sum(r['correct'] for r in accepted) / len(accepted) if accepted else None,
                'successful_cycles': sum('opponent_action' in r and not r['error'] for r in report['cycles']),
                'runtime_pids': sorted(p for p in pids if p is not None),
                'one_resident_process': len(pids) == 1 and None not in pids and all(r.get('runtime_reused') for r in responses[1:]),
                'inference_p50_seconds': statistics.median(times) if times else None,
                'device_peak_memory_mib': max(report['memory_mib'], default=None)}
            atomic_json(args.output, report)
            print(json.dumps(report['summary'], indent=2), flush=True)
    return app


if __name__ == '__main__':
    main()
