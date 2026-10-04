"""Sampled, isolated twelve-arm prompting trial, selection, and sealed confirmation.

Run: uv run --offline python -m scripts.evaluate_prompting
Resume the same frozen protocol with --resume. No model downloads or gameplay writes.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import signal
import socket
import statistics
import subprocess
import threading
import time
from urllib.parse import urlsplit

from PyQt6.QtWidgets import QApplication

from app.config import load_config
from app.games.tic_tac_toe.game import CELLS, TicTacToe
from app.games.tic_tac_toe.prompting import (
    ARMS, VERSION, candidate_facts, decision_request, fork_cells, normalize_choice, winning_cells,
)
from app.inference.imajev_client import ImajevClient
from app.ui.rendering import observation_png
from scripts.evaluate import oracle, timings

GAME = TicTacToe()
ROOT = Path(__file__).resolve().parents[1]
SEED = 20261004
ORDER = ('place_B2', 'place_A1', 'place_C1', 'place_A3', 'place_C3',
         'place_B1', 'place_A2', 'place_C2', 'place_B3')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def reachable(starter):
    seen = set()

    def visit(state):
        if state.board in seen:
            return
        seen.add(state.board)
        if GAME.outcome(state).kind != 'ongoing':
            return
        if state.next_player == 'O':
            yield state
        for action in GAME.legal_actions(state):
            yield from visit(GAME.apply_action(state, action.id))

    yield from visit(GAME.initial_state_for_player(starter))


def targets(state):
    actions = GAME.legal_actions(state)
    scores = {a.id: oracle(after.board, after.next_player) for a in actions
              for after in (GAME.apply_action(state, a.id),)}
    wins = {f'place_{CELLS[i]}' for i in winning_cells(state.board, 'O')}
    threats = winning_cells(state.board, 'X')
    blocks = {a.id for a in actions
              if not candidate_facts(state.board, CELLS.index(a.id[6:]))['allows_X_win_next_turn']}
    return {'optimal': sorted(a for a, score in scores.items() if score == max(scores.values())),
            'wins': sorted(wins), 'blocks': sorted(blocks) if threats and not wins else [],
            'required_block': bool(threats and blocks and not wins),
            'forced_loss': max(scores.values()) == -1}


def category(state):
    truth = targets(state)
    if truth['wins']:
        return 'win'
    if truth['required_block']:
        return 'block'
    if truth['forced_loss']:
        return 'forced_loss'
    if fork_cells(state.board, 'O', dangerous=True):
        return 'create_fork'
    if fork_cells(state.board, 'X', dangerous=True):
        return 'prevent_fork'
    return 'quiet'


def case(name, state, source):
    return {'id': name, 'source': source, 'category': category(state),
            'state': GAME.encode_state(state)}


def prepare_dataset(previous, seed):
    diagnostic, seen = [], set()
    if not previous.exists():
        raise ValueError(f'Prior diagnostic dataset is missing: {previous}')
    for row in json.loads(previous.read_text())['positions']:
        state = GAME.decode_state(row['state'])
        if state.board not in seen:
            diagnostic.append(case(row['id'], state, 'previous_probe'))
            seen.add(state.board)
    quoted = GAME.initial_state()
    for cell in ('B2', 'C2', 'C3'):
        quoted = GAME.apply_action(quoted, f'place_{cell}')
    if quoted.board not in seen:
        diagnostic.append(case('quoted-example', quoted, 'quoted_example'))
        seen.add(quoted.board)
    else:
        next(r for r in diagnostic if tuple(r['state']['board']) == quoted.board)['quoted_example'] = True
    pools = defaultdict(list)
    for starter in ('X', 'O'):
        for state in reachable(starter):
            if state.board not in seen and len(GAME.legal_actions(state)) > 1:
                pools[(starter, category(state))].append(state)
    rng = random.Random(seed)
    for pool in pools.values():
        rng.shuffle(pool)
    keys = sorted(pools)

    def sample(count, label):
        selected = []
        while len(selected) < count:
            for key in keys:
                if not pools[key]:
                    continue
                state = pools[key].pop()
                selected.append(case(f'{label}-{len(selected) + 1:03}', state, label))
                if len(selected) == count:
                    return selected
            if not any(pools.values()):
                raise ValueError('Not enough disjoint sampled positions.')

    diagnostic.extend(sample(64, 'fresh_diagnostic'))
    heldout = sample(256, 'holdout')
    robustness = [row['id'] for row in rng.sample(heldout, 64)]
    return {'version': 1, 'seed': seed, 'diagnostic': diagnostic, 'holdout': heldout,
            'robustness_ids': robustness}


def score_answer(state, answer):
    truth = targets(state)
    choice = normalize_choice(answer['choice'])
    legal = choice in {a.id for a in GAME.legal_actions(state)}
    accepted = legal and not answer['abstained']
    return {**truth, 'action': choice, 'legal': legal, 'accepted': accepted,
            'ranked_optimal': legal and choice in truth['optimal'],
            'accepted_optimal': accepted and choice in truth['optimal'],
            'missed_win': bool(truth['wins']) and not (accepted and choice in truth['wins']),
            'missed_block': truth['required_block'] and not (accepted and choice in truth['blocks'])}


class OwnedService:
    def __init__(self, config, output):
        self.config, self.output, self.process, self.log = config, output, None, None

    def __enter__(self):
        parts = urlsplit(self.config.endpoint)
        if parts.hostname != '127.0.0.1' or parts.port != 8765 or self.config.expected_model != 'imajev-4b-nf4':
            raise ValueError('This fixed protocol requires the local NF4 service on 127.0.0.1:8765.')
        with socket.socket() as probe:
            if probe.connect_ex((parts.hostname, parts.port)) == 0:
                raise RuntimeError('Port 8765 is occupied; the experiment will not attach or terminate its owner.')
        self.log = (self.output / 'service.log').open('a')
        try:
            self.process = subprocess.Popen(['bash', str(ROOT / 'scripts/launch_inference_4b_nf4.sh')],
                                            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
        except BaseException:
            self.log.close()
            raise
        return self

    def __exit__(self, *_):
        try:
            if self.process and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=10)
        finally:
            if self.log:
                self.log.close()


class Trial:
    def __init__(self, client, config, output):
        self.client, self.config, self.output = client, config, output
        self.calls = {}
        self.images = {}
        path = output / 'calls.jsonl'
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row['label'] in self.calls:
                    raise ValueError('Duplicate call label in checkpoint.')
                self.calls[row['label']] = row

    def call(self, state, arm, label, order='canonical'):
        actions = list(GAME.legal_actions(state))
        if len(actions) <= 1:
            raise ValueError('Forced moves must bypass the model.')
        if order == 'reverse':
            actions.reverse()
        elif order == 'shuffle':
            random.Random(digest(json_bytes(state.board))).shuffle(actions)
        request = decision_request(state, tuple(actions), arm)
        state_hash = digest(json_bytes(GAME.encode_state(state)))
        if state_hash not in self.images:
            png = observation_png(GAME.render(state, (), 'decision'), self.config.observation_size)
            image_hash = digest(png)
            path = self.output / 'images' / f'{image_hash}.png'
            if not path.exists():
                path.write_bytes(png)
            self.images[state_hash] = (png, image_hash)
        png, image_hash = self.images[state_hash]
        # Candidate insertion order changes the served decision prompt.
        request_hash = digest(json.dumps(request, allow_nan=False).encode())
        if label in self.calls:
            row = self.calls[label]
            if (row['request_sha256'] != request_hash or row['image_sha256'] != image_hash
                    or row['arm'] != arm or row['order'] != order):
                raise ValueError('Checkpoint request/image mismatch.')
            return row
        started = time.monotonic()
        reply = self.client.decide(request, png, timeout=self.config.request_timeout)
        elapsed = time.monotonic() - started
        answer = reply.answers['move']
        value = {'choice': answer.choice, 'abstained': answer.abstained,
                 'unknown_probability': answer.unknown_probability,
                 'probabilities': dict(answer.probabilities)}
        row = {'label': label, 'arm': arm, 'order': order, 'seconds': elapsed,
               'state_sha256': state_hash, 'board': state.board, 'starter': state.starting_player,
               'request_sha256': request_hash, 'image_sha256': image_hash,
               'request': request, 'reply': dict(reply.raw), 'answer': value,
               'score': score_answer(state, value)}
        with (self.output / 'calls.jsonl').open('a') as stream:
            stream.write(json.dumps(row, allow_nan=False) + '\n')
            stream.flush()
        self.calls[label] = row
        print(json.dumps({'call': len(self.calls), 'label': label, 'choice': value['choice'],
                          'accepted_optimal': row['score']['accepted_optimal'],
                          'abstained': value['abstained'], 'seconds': round(elapsed, 2)}), flush=True)
        return row

    def positions(self, rows, arms, phase, orders=('canonical',)):
        # Shared state/image and interleaved arms reduce temporal confounding.
        for row in rows:
            state = GAME.decode_state(row['state'])
            for order in orders:
                for arm in arms:
                    self.call(state, arm, f'{phase}:{row["id"]}:{order}:{arm}', order)

    def games(self, arms, phase, tie_seed, guarded=False):
        trials = []
        openings = [('X', cell) for cell in CELLS] + [('O', None)] * 3
        for arm in arms:
            for index, (starter, opening) in enumerate(openings):
                state = GAME.initial_state_for_player(starter)
                tie_order = list(ORDER)
                random.Random(tie_seed + index).shuffle(tie_order)
                trial = {'arm': arm, 'starter': starter, 'opening': opening,
                         'tie_order': tie_order, 'guarded': guarded, 'history': [], 'aborted': None}
                if opening:
                    state = GAME.apply_action(state, f'place_{opening}')
                    trial['history'].append({'player': 'X', 'action': f'place_{opening}'})
                while GAME.outcome(state).kind == 'ongoing':
                    actions = GAME.legal_actions(state)
                    if state.next_player == 'X':
                        scores = {a.id: oracle(after.board, after.next_player) for a in actions
                                  for after in (GAME.apply_action(state, a.id),)}
                        action = next(a for a in tie_order if scores.get(a) == min(scores.values()))
                        move = {'player': 'X', 'action': action}
                    elif len(actions) == 1:
                        action = actions[0].id
                        move = {'player': 'O', 'action': action, 'forced': True}
                    else:
                        label = f'{phase}:{arm}:{index}:{state.revision}'
                        row = self.call(state, arm, label)
                        action = row['score']['action']
                        move = {'player': 'O', 'action': action, 'call': label}
                        if not row['score']['accepted']:
                            trial['aborted'] = 'abstention' if row['answer']['abstained'] else 'illegal reply'
                        elif guarded:
                            action, correction = GAME.tactical_choice(state, action)
                            move.update(action=action, proposed=row['score']['action'], correction=correction)
                    trial['history'].append(move)
                    if trial['aborted']:
                        break
                    state = GAME.apply_action(state, action)
                trial.update(outcome=GAME.outcome(state).kind, winner=GAME.outcome(state).winner,
                             final_state=GAME.encode_state(state))
                trials.append(trial)
                write_json(self.output / f'{phase}.json', trials)
                print('GAME ' + json.dumps({k: v for k, v in trial.items()
                                            if k not in ('history', 'final_state', 'tie_order')}), flush=True)
        return trials


def summarize(rows):
    return {'n': len(rows), 'accepted_optimal': sum(r['score']['accepted_optimal'] for r in rows),
            'ranked_optimal': sum(r['score']['ranked_optimal'] for r in rows),
            'abstentions': sum(r['answer']['abstained'] for r in rows),
            'illegal': sum(not r['score']['legal'] for r in rows),
            'wins_available': sum(bool(r['score']['wins']) for r in rows),
            'missed_wins': sum(r['score']['missed_win'] for r in rows),
            'blocks_required': sum(r['score']['required_block'] for r in rows),
            'missed_blocks': sum(r['score']['missed_block'] for r in rows),
            'forced_loss_n': sum(r['score']['forced_loss'] for r in rows),
            'forced_loss_optimal': sum(r['score']['forced_loss'] and r['score']['accepted_optimal'] for r in rows),
            **timings([r['seconds'] for r in rows])}


def arm_rows(trial, phase, arm):
    return [row for label, row in trial.calls.items() if label.startswith(phase + ':') and row['arm'] == arm]


def game_summary(games, arm):
    selected = [g for g in games if g['arm'] == arm]
    return {'n': len(selected), 'draws': sum(g['outcome'] == 'draw' for g in selected),
            'wins': sum(g['winner'] == 'O' for g in selected),
            'losses': sum(g['winner'] == 'X' for g in selected),
            'aborts': sum(bool(g['aborted']) for g in selected)}


def select_candidate(trial, games):
    def key(arm):
        scores = summarize(arm_rows(trial, 'diagnostic', arm))
        outcomes = game_summary(games, arm)
        return (scores['accepted_optimal'], -scores['missed_wins'], -scores['missed_blocks'],
                -scores['abstentions'], outcomes['draws'] + outcomes['wins'],
                -outcomes['aborts'], -scores['p50_seconds'])
    return max((arm for arm in ARMS if arm not in ('legacy', 'raw')), key=key)


def bootstrap(candidate, baseline, seed):
    by_id = lambda rows: {r['label'].split(':')[1]: r['score']['accepted_optimal'] for r in rows}
    a, b = by_id(candidate), by_id(baseline)
    if not a or a.keys() != b.keys():
        raise ValueError('Paired bootstrap requires identical nonempty cases.')
    differences = [int(a[k]) - int(b[k]) for k in sorted(a)]
    rng = random.Random(seed)
    draws = sorted(sum(rng.choices(differences, k=len(differences))) / len(differences) for _ in range(10000))
    return {'difference': statistics.mean(differences), 'low': draws[250], 'high': draws[9749],
            'replicates': 10000, 'seed': seed}


def adoption_gate(trial, candidate, games):
    proposed = summarize(arm_rows(trial, 'holdout', candidate))
    baseline = summarize(arm_rows(trial, 'holdout', 'legacy'))
    raw = summarize(arm_rows(trial, 'holdout', 'raw'))
    confidence = bootstrap(arm_rows(trial, 'holdout', candidate), arm_rows(trial, 'holdout', 'legacy'), SEED)
    cg, bg = game_summary(games, candidate), game_summary(games, 'legacy')
    ordered_c = summarize(arm_rows(trial, 'robustness', candidate))
    ordered_b = summarize(arm_rows(trial, 'robustness', 'legacy'))
    checks = {'paired_improvement': confidence['low'] > 0,
              'not_below_raw_baseline': proposed['accepted_optimal'] >= raw['accepted_optimal'],
              'no_win_regression': proposed['missed_wins'] <= baseline['missed_wins'],
              'no_block_regression': proposed['missed_blocks'] <= baseline['missed_blocks'],
              'no_abstention_regression': proposed['abstentions'] <= baseline['abstentions'],
              'no_illegal_replies': proposed['illegal'] == 0 and ordered_c['illegal'] == 0,
              'no_game_regression': cg['draws'] + cg['wins'] >= bg['draws'] + bg['wins'] and cg['aborts'] <= bg['aborts'],
              'no_order_regression': ordered_c['accepted_optimal'] >= ordered_b['accepted_optimal'],
              'latency_within_2x': proposed['p50_seconds'] <= 2 * baseline['p50_seconds']}
    return {'candidate': candidate, 'adopt': all(checks.values()), 'checks': checks,
            'paired_bootstrap_95': confidence}


def protected_hashes():
    paths = [ROOT / 'config.4b-nf4.yaml', *ROOT.glob('imajev-*.json')]
    paths.extend((ROOT / 'learning').rglob('*.json'))
    return {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in paths if p.is_file()}


def render_report(summary, dataset, output):
    lines = ['# Context and output prompting experiment', '',
             f'Candidate: **{summary["decision"]["candidate"]}**. '
             f'Adoption gate: **{"passed" if summary["decision"]["adopt"] else "failed"}**.', '',
             'Pinned Qwen3.5-4B NF4, ImaJEV adapter and 256-code readout, existing calibration, '
             'four rotations, no thinking. No coaching, retained strategy, retries or tactical corrections '
             'in position comparisons. Local consequences are supplied only in the designated arms; '
             'minimax is used only for scoring and perfect X.', '',
             '## Position comparisons', '',
             '| Phase | Arm | Accepted optimal | Ranked optimal | Missed wins | Missed blocks | Abstentions | p50 / p95 seconds |',
             '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for phase in ('diagnostic', 'holdout', 'robustness'):
        for arm, value in summary[phase].items():
            lines.append(f'| {phase} | {arm} | {value["accepted_optimal"]}/{value["n"]} | '
                         f'{value["ranked_optimal"]}/{value["n"]} | '
                         f'{value["missed_wins"]}/{value["wins_available"]} | '
                         f'{value["missed_blocks"]}/{value["blocks_required"]} | '
                         f'{value["abstentions"]} | {value["p50_seconds"]:.2f} / {value["p95_seconds"]:.2f} |')
    lines += ['', '## Games against perfect X', '',
              '| Phase | Arm | Draws | O wins | Losses | Aborts |',
              '| --- | --- | --- | --- | --- | --- |']
    for phase, arms in summary['games'].items():
        for arm, value in arms.items():
            lines.append(f'| {phase} | {arm} | {value["draws"]} | {value["wins"]} | {value["losses"]} | {value["aborts"]} |')
    lines += ['', '## Quoted example', '',
              'Board: `. . . / . X O / . . X`. A1 is the required diagonal block.', '',
              '| Arm | Ranked choice | Abstained | Block taken |', '| --- | --- | --- | --- |']
    for arm, value in summary['quoted_example'].items():
        lines.append(f'| {arm} | {value["choice"]} | {value["abstained"]} | {value["block_taken"]} |')
    ci = summary['decision']['paired_bootstrap_95']
    lines += ['', '## Adoption decision', '',
              f'Holdout accepted-optimal difference versus current gameplay: {ci["difference"]:.1%}; '
              f'paired-bootstrap 95% interval [{ci["low"]:.1%}, {ci["high"]:.1%}] (10,000 resamples).', '']
    for name, passed in summary['decision']['checks'].items():
        lines.append(f'- {name}: {"pass" if passed else "fail"}.')
    lines += ['', '## Protocol and limitations', '',
              f'Diagnostic positions: {len(dataset["diagnostic"])}; holdout: {len(dataset["holdout"])}. '
              'Both starters are represented. Historical probes retain their recorded drawings. '
              'The candidate was frozen before holdout calls. Each arm plays all nine X openings and '
              'three O-start games; confirmation uses fresh fixed tie orders. Order robustness uses '
              '64 holdout boards with reversed and seeded shuffled options.', '',
              'Abstentions count as failed accepted decisions and abort raw games. Forced one-option '
              'turns bypass inference. Forced-loss boards are reported separately in summary.json; '
              'optimality on them does not mean a safe move. The sample is stratified, not a natural '
              'game distribution or exhaustive model validation. Diagnostic prompt selection uses no holdout results.', '',
              'Facts are bounded tactical simulations, not a minimax answer. A fork creates at least '
              'two distinct next-turn winning cells. Opponent fork prevention includes forcing replies: '
              'forks allowing an immediate O win are not dangerous. Immediate-win and terminal states '
              'do not claim subsequent opponent replies. Structured and text descriptions flatten '
              'to identical strings in the pinned API; their separate calls serve as a representation control.', '',
              f'Measured requests: {summary["calls"]}. Peak sampled whole-device GPU use: '
              f'{summary["peak_device_mib"]} MiB. Protected local-file hashes preserved: '
              f'{summary["protected_preserved"]}.', '',
              '## Reproduction', '',
              '```bash', 'uv run --offline python -m scripts.evaluate_prompting',
              'uv run --offline python -m scripts.evaluate_prompting --resume', '```', '',
              f'Raw artifacts: `{output}`. The directory contains the frozen '
              'dataset, protocol, selection checkpoint, calls.jsonl, exact PNGs, game histories, '
              'summary, resource readings and owned-service log. Resume validates code, configuration, '
              'runtime assets, dataset and request/image hashes. Fresh trials require a fresh output directory.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'logs/prompting-strategy-experiment')
    parser.add_argument('--previous', type=Path, default=ROOT / 'logs/decomposition-experiment/results.json')
    parser.add_argument('--seed', type=int, default=SEED)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'images').mkdir(exist_ok=True)
    app = QApplication.instance() or QApplication([])
    config = load_config(ROOT / 'config.yaml')
    config = replace(config, request_timeout=180)
    source_files = [Path(__file__), ROOT / 'app/games/tic_tac_toe/prompting.py',
                    ROOT / 'app/games/tic_tac_toe/game.py', ROOT / 'scripts/evaluate.py',
                    ROOT / 'app/inference/imajev_client.py', ROOT / 'scripts/serve_local.py',
                    ROOT / 'scripts/launch_inference_4b_nf4.sh', ROOT / 'app/ui/rendering.py']
    protocol = {'version': VERSION, 'seed': args.seed, 'arms': ARMS,
                'config_sha256': digest((ROOT / 'config.yaml').read_bytes()),
                'code_sha256': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in source_files},
                'runtime': json.loads((ROOT / '.inference/runtime-manifest-4b-nf4.json').read_text()),
                'previous_sha256': digest(args.previous.read_bytes()),
                'rotations': 4, 'thinking': 'off', 'request_timeout_seconds': 180,
                'selection': 'accepted optimal, wins, blocks, abstentions, nonloss games, game aborts, latency',
                'bootstrap_seed': SEED}
    if (output / 'protocol.json').exists():
        if not args.resume:
            parser.error('Output already contains a protocol. Use --resume or a fresh directory.')
        if json_bytes(json.loads((output / 'protocol.json').read_text())) != json_bytes(protocol):
            parser.error('Protocol/code/config/assets mismatch: use a fresh output directory.')
        dataset = json.loads((output / 'dataset.json').read_text())
        if digest(json_bytes(dataset)) != (output / 'dataset.sha256').read_text().strip():
            parser.error('Dataset checkpoint hash mismatch.')
    else:
        if args.resume:
            parser.error('Cannot resume without a saved protocol.')
        dataset = prepare_dataset(args.previous, args.seed)
        write_json(output / 'dataset.json', dataset)
        (output / 'dataset.sha256').write_text(digest(json_bytes(dataset)) + '\n')
        write_json(output / 'protocol.json', protocol)
        write_json(output / 'protected-before.json', protected_hashes())
    print('DATASET ' + json.dumps({phase: dict(Counter(
        f'{row["state"]["starting_player"]}:{row["category"]}' for row in dataset[phase]))
        for phase in ('diagnostic', 'holdout')}), flush=True)
    if args.prepare_only:
        return
    stop = threading.Event()
    resources = output / 'resources.json'
    readings = json.loads(resources.read_text()) if args.resume and resources.exists() else []

    def monitor():
        while not stop.is_set():
            try:
                value = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used',
                                                 '--format=csv,noheader,nounits'], text=True, timeout=3)
                readings.append({'utc': time.time(), 'device_mib': int(value.strip().splitlines()[0])})
                if len(readings) % 12 == 0:
                    write_json(output / 'resources.json', readings)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            stop.wait(5)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    started = time.monotonic()
    client = ImajevClient(config)
    trial = Trial(client, config, output)
    try:
        with OwnedService(config, output):
            state = GAME.initial_state_for_player('O')
            warmup = client.warmup(decision_request(state, GAME.legal_actions(state), 'raw'),
                                   observation_png(GAME.render(state, (), 'decision'), config.observation_size))
            write_json(output / 'warmup.json', dict(warmup.raw))
            print('READY', flush=True)
            trial.positions(dataset['diagnostic'], ARMS, 'diagnostic')
            diagnostic_games = trial.games(ARMS, 'diagnostic-games', args.seed)
            chosen = select_candidate(trial, diagnostic_games)
            selection = {'candidate': chosen, 'diagnostic_calls_sha256': digest(json_bytes([
                (r['label'], r['answer']) for r in trial.calls.values() if r['label'].startswith('diagnostic:')]))}
            path = output / 'selection.json'
            if path.exists() and json_bytes(json.loads(path.read_text())) != json_bytes(selection):
                raise ValueError('Frozen candidate selection changed.')
            write_json(path, selection)
            print('SELECTED ' + chosen, flush=True)
            finalists = ('legacy', 'raw', chosen)
            trial.positions(dataset['holdout'], finalists, 'holdout')
            robust = [r for r in dataset['holdout'] if r['id'] in dataset['robustness_ids']]
            trial.positions(robust, finalists, 'robustness', ('reverse', 'shuffle'))
            confirmation = trial.games(finalists, 'confirmation-games', args.seed + 10000)
            assisted = trial.games(finalists, 'assisted-games', args.seed + 10000, guarded=True)
            summary = {phase: {arm: summarize(arm_rows(trial, phase, arm)) for arm in arms}
                       for phase, arms in [('diagnostic', ARMS), ('holdout', finalists), ('robustness', finalists)]}
            summary['decision'] = adoption_gate(trial, chosen, confirmation)
            summary['games'] = {phase: {arm: game_summary(games, arm) for arm in arms}
                                for phase, games, arms in [('diagnostic', diagnostic_games, ARMS),
                                                          ('confirmation', confirmation, finalists),
                                                          ('assisted', assisted, finalists)]}
            quoted_board = ('', '', '', '', 'X', 'O', '', '', 'X')
            summary['quoted_example'] = {arm: {'choice': row['answer']['choice'],
                'abstained': row['answer']['abstained'], 'block_taken': not row['score']['missed_block']}
                for arm in ARMS for row in arm_rows(trial, 'diagnostic', arm) if tuple(row['board']) == quoted_board}
        summary.update(calls=len(trial.calls), elapsed_this_run_seconds=time.monotonic() - started,
                       peak_device_mib=max((r['device_mib'] for r in readings), default=None),
                       protected_preserved=protected_hashes() == json.loads((output / 'protected-before.json').read_text()),
                       dataset_sha256=digest(json_bytes(dataset)), protocol_sha256=digest(json_bytes(protocol)))
        write_json(output / 'summary.json', summary)
        (output / 'report.md').write_text(render_report(summary, dataset, output))
        print('DECISION ' + json.dumps(summary['decision']), flush=True)
    except BaseException as exc:
        write_json(output / 'interruption.json', {'error': repr(exc), 'calls': len(trial.calls),
                   'elapsed_seconds': time.monotonic() - started, 'protected_preserved':
                   protected_hashes() == json.loads((output / 'protected-before.json').read_text())})
        raise
    finally:
        stop.set()
        thread.join(timeout=4)
        write_json(output / 'resources.json', readings)


if __name__ == '__main__':
    main()
