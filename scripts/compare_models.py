"""Compare configured opponents on frozen game positions without coaching writes."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from app.config import load_config
from app.core.move_sampling import select_move
from app.core.registry import get_game
from app.inference.opponents import create_opponent, metadata, Unsupported
from app.inference.service_manager import ServiceManager
from app.inference.provenance import file_identity
from app.ui.rendering import observation_png
from scripts.evaluate import o_turn_states, oracle

VERSION = 'opponent-comparison-v1'
ROOT = Path(__file__).resolve().parents[1]


def encoded(value):
    # Preserve option insertion order: order is part of the experiment.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(encoded(value))
    temporary.replace(path)


def states(game):
    if game.id == 'tic_tac_toe':
        yield from o_turn_states()
        return
    seen = set()
    for path in sorted((ROOT / 'docs/evaluation/fixtures/boku').glob('*session.json')):
        for event in json.loads(path.read_text()).get('events', []):
            record = event.get('state')
            if not record:
                continue
            state = game.decode_state(record)
            key = (state.board, state.next_player, state.phase, state.forbidden, state.reserves)
            if key in seen or game.current_player(state) != game.computer_player or game.outcome(state).kind != 'ongoing':
                continue
            seen.add(key)
            yield state


def evidence(game, state):
    actions = game.legal_actions(state)
    if game.id == 'tic_tac_toe':
        scores = {a.id: oracle(end.board, end.next_player) for a in actions
                  for end in [game.apply_action(state, a.id)]}
        wins = [a.id for a in actions if game.outcome(game.apply_action(state, a.id)).winner == 'O']
        from dataclasses import replace
        threats = any(game.outcome(game.apply_action(replace(state, next_player='X'), a.id)).winner == 'X' for a in actions)
        blocks = []
        for a in actions:
            end = game.apply_action(state, a.id)
            if all(game.outcome(game.apply_action(end, reply.id)).winner != 'X' for reply in game.legal_actions(end)):
                blocks.append(a.id)
        return {'kind': 'exact_minimax', 'scores': scores,
                'optimal': [a for a, score in scores.items() if score == max(scores.values())],
                'wins': wins, 'necessary_blocks': blocks if threats and blocks and not wins else []}
    from app.games.boku.tactics import candidate_facts
    facts = candidate_facts(state, actions)
    return {'kind': 'bounded_tactical_facts', 'facts': facts,
            'wins': [a for a, f in facts.items() if f.get('wins_now')],
            'necessary_blocks': [a for a, f in facts.items() if f.get('blocks_Black_win_next_turn')]}


def score(choice, truth):
    result = {'missed_win': bool(truth['wins']) and choice not in truth['wins'],
              'missed_necessary_block': bool(truth['necessary_blocks']) and not truth['wins'] and choice not in truth['necessary_blocks']}
    if truth['kind'] == 'exact_minimax':
        result.update(optimal=choice in truth['optimal'], losing_move=truth['scores'].get(choice) == -1)
    else:
        facts = truth['facts'].get(choice, {})
        result.update(allows_immediate_loss=bool(facts.get('allows_Black_win_next_turn')),
                      allows_bounded_forced_loss=bool(facts.get('allows_Black_forced_win')))
    return result


def prepare(configs, game, limit, seed, output):
    pool = list(states(game))
    random.Random(seed).shuffle(pool)
    if limit:
        pool = pool[:limit]
    cases = []
    for index, state in enumerate(pool):
        png = observation_png(game.render(state, (), 'decision'), configs[0].observation_size)
        image_hash = digest(png)
        (output / 'images' / (image_hash + '.png')).write_bytes(png)
        actions = list(game.legal_actions(state))
        truth = evidence(game, state)
        for order in ('canonical', 'reverse', 'shuffle'):
            ordered = actions.copy()
            if order == 'reverse':
                ordered.reverse()
            elif order == 'shuffle':
                random.Random(f'{seed}:{index}').shuffle(ordered)
            kwargs = {'prompt_variant': configs[0].prompt_variant}
            if game.id == 'tic_tac_toe':
                kwargs['opening_suggestion'] = configs[0].opening_suggestion
            request = game.decision_request(state, tuple(ordered), **kwargs)
            cases.append({'id': f'{index:04}:{order}', 'state': game.encode_state(state),
                          'request': request, 'request_sha256': digest(encoded(request)),
                          'image_sha256': image_hash, 'order': order, 'evidence': truth})
    return cases


def memory():
    """Device-wide snapshot, not a claim of per-process peak consumption."""
    try:
        values = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], text=True, timeout=3)
        return {'gpu_device_mib': [int(v) for v in values.splitlines()]}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'gpu_device_mib': None}


def run_case(game, config, opponent, case, png, seed):
    state = game.decode_state(case['state'])
    row = {'case': case['id'], 'request_sha256': case['request_sha256'],
           'image_sha256': case['image_sha256'], 'order': case['order'], 'status': 'error'}
    readings = []
    stop = threading.Event()
    def monitor():
        while not stop.is_set():
            readings.append(memory())
            stop.wait(.5)
    sampler = threading.Thread(target=monitor, daemon=True)
    sampler.start()
    started = time.monotonic()
    try:
        actions = game.legal_actions(state)
        if len(actions) == 1:
            row.update(status='forced', raw_proposal=None, selected=actions[0].id)
        else:
            reply = opponent.choose_move(case['request'], png)
            row.update(raw_proposal=reply.choice, move_result=metadata(reply), raw_response=dict(reply.raw))
            proposed, selected, distribution = select_move(game, state, reply, config.move_temperature, random.Random(f'{seed}:{case["id"]}'))
            row['raw_score'] = score(proposed, case['evidence'])
            correction = None
            if config.tactical_guard and hasattr(game, 'tactical_choice'):
                selected, correction = game.tactical_choice(state, selected)
            game.apply_action(state, selected)
            row.update(status='valid', selected=selected, tactical_correction=correction,
                       sampled_distribution=distribution, assisted_score=score(selected, case['evidence']))
            row['activation_seconds'] = reply.raw.get('activation_seconds')
            row['inference_seconds'] = reply.raw.get('inference_seconds')
    except Unsupported as exc:
        row.update(status='unsupported', error=str(exc))
    except Exception as exc:
        row.update(status='refusal' if row.get('move_result', {}).get('abstained') else 'error', error=str(exc))
    row['total_seconds'] = time.monotonic() - started
    stop.set()
    sampler.join(timeout=4)
    readings.append(memory())
    measured = [r['gpu_device_mib'] for r in readings if r['gpu_device_mib'] is not None]
    row['memory'] = {'gpu_device_mib': readings[-1]['gpu_device_mib'],
                     'sampled_peak_gpu_device_mib': [max(r[i] for r in measured) for i in range(len(measured[0]))] if measured else None,
                     'samples': len(readings), 'scope': 'whole device, sampled; excludes Windows host RAM'}
    return row


def summarize(rows):
    eligible = [r for r in rows if r['status'] != 'forced']
    supported = [r for r in eligible if r['status'] != 'unsupported']
    valid = [r for r in supported if r['status'] == 'valid']
    result = {'total': len(rows), 'model_cases': len(eligible), 'supported_cases': len(supported),
              'valid_cases': len(valid), 'coverage': len(supported) / len(eligible) if eligible else None,
              'valid_rate_supported': len(valid) / len(supported) if supported else None,
              'valid_rate_all': len(valid) / len(eligible) if eligible else None,
              'statuses': {s: sum(r['status'] == s for r in rows) for s in ('valid', 'forced', 'unsupported', 'refusal', 'error')}}
    for name in ('raw_score', 'assisted_score'):
        keys = {key for r in valid for key in r.get(name, {})}
        result[name] = {key: sum(bool(r[name].get(key)) for r in valid) / len(valid) for key in sorted(keys)}
    from scripts.evaluate import timings
    result.update(timings([r['total_seconds'] for r in valid]))
    groups = {}
    for row in valid:
        groups.setdefault(row['case'].split(':')[0], []).append(row)
    triples = [group for group in groups.values() if len(group) == 3]
    result['option_order_complete_positions'] = len(triples)
    result['option_order_changed_proposal_rate'] = sum(len({r['raw_proposal'] for r in group}) > 1 for group in triples) / len(triples) if triples else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, action='append', required=True)
    parser.add_argument('--game', choices=('tic_tac_toe', 'boku'), default='tic_tac_toe')
    parser.add_argument('--limit', type=int, default=32, help='Positions; 0 uses every position.')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative.')
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    configs = [load_config(path) for path in args.config]
    shared = lambda c: (c.prompt_variant, c.opening_suggestion, c.observation_size, c.tactical_guard)
    if any(shared(c) != shared(configs[0]) for c in configs):
        parser.error('Comparisons require identical prompts, observation size, opening suggestions and tactical guards.')
    game = get_game(args.game)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    (output / 'images').mkdir(exist_ok=True)
    local_assets = [[file_identity(path) for path in (c.opponent_settings.model_path, c.opponent_settings.projector_path) if path and Path(path).is_file()] for c in configs]
    protocol = {'version': VERSION, 'game': args.game, 'prompt_version': game.prompt_version,
                'seed': args.seed, 'limit': args.limit, 'configs': [asdict(c) for c in configs], 'local_assets': local_assets,
                'code': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for folder in ('app', 'scripts') for p in sorted((ROOT / folder).rglob('*.py'))}}
    # JSON normalizes paths and tuples consistently across resume.
    protocol = json.loads(json.dumps(protocol, default=str))
    if (output / 'protocol.json').exists():
        if not args.resume or json.loads((output / 'protocol.json').read_text()) != protocol:
            parser.error('Existing experiment differs or --resume was omitted. Use a new output directory.')
        cases = json.loads((output / 'cases.json').read_text())
        expected = (output / 'cases.sha256').read_text().strip()
        if digest(encoded(cases)) != expected:
            parser.error('Frozen cases were modified.')
    else:
        if args.resume:
            parser.error('No experiment exists to resume.')
        cases = prepare(configs, game, args.limit, args.seed, output)
        save(output / 'protocol.json', protocol)
        save(output / 'cases.json', cases)
        (output / 'cases.sha256').write_text(digest(encoded(cases)) + '\n')
    for case in cases:
        png = (output / 'images' / (case['image_sha256'] + '.png')).read_bytes()
        if digest(png) != case['image_sha256'] or digest(encoded(case['request'])) != case['request_sha256']:
            parser.error('Frozen image or prompt hash differs.')
    if args.prepare_only:
        print(f'Prepared {len(cases)} cases in {output}')
        return
    summaries = []
    for index, config in enumerate(configs):
        path = output / f'model-{index}.json'
        rows = json.loads(path.read_text()) if path.exists() else []
        checkpoint_hash = path.with_suffix('.sha256')
        if rows and (not checkpoint_hash.exists() or checkpoint_hash.read_text().strip() != digest(encoded(rows))):
            parser.error('Model checkpoint hash differs.')
        if len(rows) > len(cases) or any(r['case'] != c['id'] or r['request_sha256'] != c['request_sha256'] or r['image_sha256'] != c['image_sha256'] for r, c in zip(rows, cases)):
            parser.error('Checkpoint does not match frozen cases.')
        manager = ServiceManager(config)
        opponent = create_opponent(config, manager=manager)
        try:
            if len(rows) < len(cases):
                # One real, supported request warms the runtime; excluded from scored trials.
                for case in cases:
                    if len(game.legal_actions(game.decode_state(case['state']))) < 2:
                        continue
                    png = (output / 'images' / (case['image_sha256'] + '.png')).read_bytes()
                    try:
                        if hasattr(opponent, 'preflight'):
                            opponent.preflight(case['request'], png)
                    except Unsupported:
                        continue
                    started = time.monotonic()
                    warm = run_case(game, config, opponent, case, png, args.seed)
                    save(output / f'warmup-{index}.json', {'seconds': time.monotonic() - started, 'result': warm})
                    previous = next((r.get('raw_response', {}).get('runtime_provenance') for r in rows if r.get('raw_response', {}).get('runtime_provenance')), None)
                    current = warm.get('raw_response', {}).get('runtime_provenance')
                    if previous is not None and previous != current:
                        raise ValueError('Runtime provenance changed since the checkpoint; use a fresh experiment.')
                    break
            for case in cases[len(rows):]:
                png = (output / 'images' / (case['image_sha256'] + '.png')).read_bytes()
                row = run_case(game, config, opponent, case, png, args.seed)
                rows.append(row)
                save(path, rows)
                checkpoint_hash.write_text(digest(encoded(rows)) + '\n')
                print(f'{config.opponent_settings.model} {case["id"]}: {row["status"]}', flush=True)
        finally:
            manager.shutdown()
        summaries.append({'model': config.opponent_settings.model, **summarize(rows)})
        save(output / 'summary.json', summaries)


if __name__ == '__main__':
    main()
