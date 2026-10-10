"""Compare saved baseline, broader facts and named defence on Boku loss fixtures."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from PyQt6.QtWidgets import QApplication
from app.config import load_config
from app.games.boku.game import Boku, State
from app.games.boku.geometry import CELLS
from app.inference.imajev_client import ImajevClient, InferenceError
from app.storage.atomic import atomic_json
from app.ui.rendering import observation_png


FIXTURES = Path('docs/evaluation/fixtures/boku')
DEFENCE_INSTRUCTION = (
    ' Otherwise, if White_defensive_actions is nonempty, choose one action from that list. '
    'These defensive actions avoid the checked immediate losses and forced-win setups; they do not guarantee safety beyond this horizon.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    parser.add_argument('--output', type=Path, default=Path('logs/boku-forced-wins.json'))
    parser.add_argument('--controls', action='store_true')
    parser.add_argument('--arms', nargs='+', choices=('baseline', 'facts_only', 'named_defence'),
                        default=['baseline', 'facts_only', 'named_defence'])
    args = parser.parse_args()
    app = QApplication([])
    game = Boku()
    saved = json.loads((FIXTURES / 'open-four-session.json').read_text())
    event = saved['events'][0]
    cases = [('open_four_D5', game.decode_state(event['state']), event['request'])]
    if args.controls:
        saved = json.loads((FIXTURES / 'missed-win-session.json').read_text())
        for rev, name in ((53, 'A1_win'), (9, 'E5_block'), (25, 'capture_trap'),
                          (28, 'F4_counter'), (29, 'E4_capture')):
            e = next(e for e in saved['events'] if e['ticket']['state_revision'] == rev)
            cases.append((name, game.decode_state(e['state']), e['request']))
        white = ('F1', 'F2', 'F3', 'F4', 'I5')
        state = State(board=tuple('White' if c in white else 'Black' if c in ('G5', 'H5') else '' for c in CELLS),
                      next_player='White', reserves=(34, 1))
        cases += [('win_requires_capture', state, None),
                  ('winning_capture', game.apply_action(state, 'place_F5'), None)]
    client = ImajevClient(replace(load_config(args.config), external_inference=True))
    result = {'source': str(FIXTURES / 'open-four-session.json'), 'rows': [], 'error': None}
    try:
        for name, state, baseline in cases:
            then = time.monotonic()
            production = game.decision_request(state, game.legal_actions(state), prompt_variant='quoted')
            prep_seconds = time.monotonic() - then
            for arm in args.arms:
                if name != 'open_four_D5' and arm != 'named_defence':
                    continue
                request = json.loads(json.dumps(baseline if arm == 'baseline' else production))
                if arm == 'facts_only':
                    request['state'].pop('White_defensive_actions')
                    instructions = request['questions']['move']['instructions']
                    assert instructions.endswith(DEFENCE_INSTRUCTION)
                    request['questions']['move']['instructions'] = instructions[:-len(DEFENCE_INSTRUCTION)]
                then = time.monotonic()
                deadline = then + 180
                while True:
                    try:
                        reply = client.decide(request, observation_png(game.render(state, (), 'decision')))
                        break
                    except InferenceError as exc:
                        if 'previous GPU request' not in str(exc) or time.monotonic() > deadline:
                            raise
                        time.sleep(1)
                selected = game.decode_decision(state, reply)
                wins = production['state']['immediate_White_win_actions']
                facts = production['questions']['move']['criteria'][selected]
                passed = selected in wins if wins else not (
                    facts.get('allows_Black_win_next_turn', False) or facts.get('allows_Black_forced_win', False))
                row = {'case': name, 'arm': arm, 'selected': selected, 'passed': passed,
                       'defensive_actions': production['state']['White_defensive_actions'],
                       'request': request, 'reply': reply.raw, 'preparation_seconds': prep_seconds,
                       'seconds': time.monotonic() - then}
                result['rows'].append(row)
                atomic_json(args.output, result)
                print(json.dumps({k: row[k] for k in ('case', 'arm', 'selected', 'passed', 'preparation_seconds', 'seconds')}), flush=True)
                if name == 'open_four_D5' and arm == 'named_defence' and passed:
                    # Play Black's attempted continuation to verify the selected
                    # defence leads to a real counter-win or the required block.
                    setup = 'place_G8' if selected == 'place_C5' else 'place_C5'
                    after = game.apply_action(game.apply_action(state, selected), setup)
                    cases.append(('open_four_follow_through', after, None))
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        atomic_json(args.output, result)
    return app


if __name__ == '__main__':
    main()
