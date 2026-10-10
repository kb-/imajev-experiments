"""Experiment with explicit loss facts, evaluation questions and acknowledgement.

Archived experiment: acknowledgement was dropped from gameplay.
Does not change app prompts, select/apply moves, or stop/start any service.
The service's implicit unknown answer remains available in every arm.
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


FIXTURES = Path('docs/evaluation/fixtures/boku')
ARMS = ('baseline', 'play_on', 'engine_fact', 'engine_fact_play_on',
        'batched_evaluation', 'multi_turn', 'loss_option', 'loss_option_with_fact')
LOSS_SCOPE = (
    'has_lost means every legal White action permits either an immediate Black win '
    'or a verified Black setup after which every complete White reply permits a Black win. '
    'This is a forced loss within the checked horizon, not an already terminal board. '
    'False means this bounded evaluation has not proved a forced loss.')
PLAY_ON = (
    ' Even if every legal action loses, choose one listed legal action to continue playing. '
    'Do not abstain solely because defeat is unavoidable.')
LOSS_OPTION_INSTRUCTION = (
    ' Choose an immediate White winning action first, otherwise a listed defensive action if available. '
    'If every legal White action permits an immediate Black win or a verified forced-win setup, '
    'choose acknowledge_loss instead of a board action. Unknown is for insufficient evidence, '
    'not for a proved unavoidable defeat.')


def has_lost(facts):
    return bool(facts) and all(
        not f.get('wins_now', False) and (
            f.get('allows_Black_win_next_turn', False) or
            f.get('allows_Black_forced_win', False)) for f in facts.values())


def loss_question():
    return {'type': 'choice', 'instructions':
            'Evaluate whether White has already lost within the checked horizon. ' + LOSS_SCOPE,
            'criteria': {'yes': 'Every legal White action allows an immediate Black win or a verified forced-win setup.',
                         'no': 'At least one legal White action avoids the checked losses, including a White win or terminal draw.'}}


def variant(original, arm, lost, previous=None):
    request = json.loads(json.dumps(original))
    if arm in ('engine_fact', 'engine_fact_play_on', 'loss_option_with_fact'):
        request['state'].update(has_lost=lost, has_lost_scope=LOSS_SCOPE)
    if arm in ('play_on', 'engine_fact_play_on', 'multi_turn'):
        request['questions']['move']['instructions'] += PLAY_ON
    if arm == 'batched_evaluation':
        request['questions'] = {'has_lost': loss_question(), **request['questions']}
    if arm == 'multi_turn':
        if previous is None:
            raise ValueError('A previous evaluation is required for the multi-turn arm.')
        answer = named_result(previous, 'has_lost')
        request['state'].update(has_lost=('unknown' if answer.abstained else answer.choice == 'yes'),
                                has_lost_scope=LOSS_SCOPE,
                                has_lost_source="Imajev's preceding evaluation; not an engine adjudication.")
    if arm in ('loss_option', 'loss_option_with_fact'):
        request['questions']['move']['criteria']['acknowledge_loss'] = (
            'Acknowledge unavoidable White defeat: every legal White action permits '
            'an immediate Black win or a verified forced-win setup.')
        request['questions']['move']['instructions'] += LOSS_OPTION_INSTRUCTION
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.boku.yaml'))
    parser.add_argument('--output', type=Path, default=Path('logs/boku-loss-acknowledgement.json'))
    parser.add_argument('--controls', action='store_true')
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS))
    args = parser.parse_args()
    app = QApplication([])
    game = Boku()
    saved = json.loads((FIXTURES / 'abstained-loss-session.json').read_text())
    event = next(e for e in saved['events'] if e['ticket']['state_revision'] == 32)
    cases = [('abstained_G8_loss', event)]
    if args.controls:
        for rev, name in ((29, 'forced_capture_loss'), (27, 'only_C5_defends')):
            cases.append((name, next(e for e in saved['events'] if e['ticket']['state_revision'] == rev)))
        old = json.loads((FIXTURES / 'open-four-session.json').read_text())
        cases.append(('double_ended_loss', old['events'][1]))
        old = json.loads((FIXTURES / 'winning-replay-session.json').read_text())
        cases.append(('A1_wins', old['events'][0]))
        old = json.loads((FIXTURES / 'missed-win-session.json').read_text())
        cases.append(('E5_defends', next(e for e in old['events'] if e['ticket']['state_revision'] == 9)))
    config = replace(load_config(args.config), external_inference=True)
    client = create_opponent(config)
    result = {'inference_protocol': 2, 'inference': role_metadata(config), 'source': str(FIXTURES / 'abstained-loss-session.json'), 'rows': [], 'error': None}
    def call(request, png):
        deadline = time.monotonic() + 180
        while True:
            try:
                return client.choose_move(request, png)
            except InferenceError as exc:
                if 'previous GPU request' not in str(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(1)
    try:
        for name, event in cases:
            state = game.decode_state(event['state'])
            original = event['request']
            # Recalculate evidence instead of trusting a persisted flag.
            production = game.decision_request(state, game.legal_actions(state), prompt_variant='quoted')
            facts = {a:f for a,f in production['questions']['move']['criteria'].items() if a != 'acknowledge_loss'}
            lost = has_lost(facts)
            png = observation_png(game.render(state, (), 'decision'))
            previous = None
            for arm in args.arms:
                evaluation = None
                then = time.monotonic()
                if arm == 'multi_turn':
                    if previous is None:
                        question = {'state': original['state'], 'questions': {'has_lost': loss_question()}}
                        previous = call(question, png)
                        evaluation = {'request': question, 'reply': previous.raw}
                    else:
                        evaluation = {'from_arm': 'batched_evaluation', 'reply': previous.raw}
                request = variant(original, arm, lost, previous)
                reply = call(request, png)
                if arm == 'batched_evaluation':
                    previous = reply
                answer = reply
                selected = answer.choice
                legal = selected in facts
                if answer.abstained:
                    disposition = 'unknown'
                elif selected == 'acknowledge_loss':
                    disposition = 'acknowledged_loss'
                else:
                    disposition = 'legal_move' if legal else 'invalid'
                wins = production['state']['immediate_White_win_actions']
                if lost:
                    passed = disposition == ('acknowledged_loss' if arm.startswith('loss_option') else 'legal_move')
                else:
                    passed = disposition == 'legal_move' and (
                        selected in wins if wins else not has_lost({selected: facts[selected]}))
                row = {'case': name, 'arm': arm, 'has_lost': lost, 'disposition': disposition,
                       'selected': selected, 'abstained': answer.abstained,
                       'unknown_probability': answer.unknown_probability, 'passed': passed,
                       'request': request, 'reply': reply.raw, 'seconds': time.monotonic() - then}
                evaluated = reply if arm == 'batched_evaluation' else previous if arm == 'multi_turn' else None
                if evaluated is not None:
                    diagnosis = named_result(evaluated, 'has_lost')
                    row['loss_evaluation_correct'] = not diagnosis.abstained and (diagnosis.choice == 'yes') == lost
                if evaluation:
                    row['previous_evaluation'] = evaluation
                result['rows'].append(row)
                atomic_json(args.output, result)
                print(json.dumps({k: row[k] for k in ('case', 'arm', 'has_lost', 'disposition', 'selected',
                                                       'unknown_probability', 'passed', 'seconds')}), flush=True)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        atomic_json(args.output, result)
    return app


if __name__ == '__main__':
    main()
