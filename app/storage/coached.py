"""Ordered rules and completed history, isolated from old learning experiments."""
from collections import Counter
import json
import os
from pathlib import Path
import uuid

BASIC_QUOTED_STRATEGY = ['win immediately', 'otherwise stop X winning next turn']


def validate_strategy(strategy):
    if (not isinstance(strategy, list) or not 1 <= len(strategy) <= 12
            or any(not isinstance(rule, str) or not rule.strip() or len(rule) > 160 for rule in strategy)
            or len(json.dumps(strategy, ensure_ascii=False).encode()) > 2048):
        raise ValueError('Strategy must contain 1–12 nonempty rules, at most 160 characters each and 2048 bytes overall.')
    return [rule.strip() for rule in strategy]


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CoachedStore:
    def __init__(self, root):
        self.root = Path(root)

    def ledger(self):
        path = self.root / 'strategy.json'
        if not path.exists():
            return {'version': 1, 'revision': 0, 'strategy': BASIC_QUOTED_STRATEGY.copy(),
                    'consumed_game_ids': [], 'revisions': []}
        ledger = json.loads(path.read_text())
        if ledger.get('version') != 1:
            raise ValueError('Unsupported coached strategy ledger.')
        validate_strategy(ledger['strategy'])
        return ledger

    def updated(self, game_id):
        return any(r['trigger_game_id'] == game_id for r in self.ledger()['revisions'])

    def save(self, record, game, state):
        atomic_json(self.root / 'sessions' / record['session_id'] / 'session.json', record)
        outcome = game.outcome(state)
        if outcome.kind == 'ongoing':
            return
        path = self.root / 'games' / (record['session_id'] + '.json')
        if path.exists():
            return  # Completion timestamp stays stable through resume and diagnostics changes.
        turns = {}
        for event in record['events']:
            ticket = event.get('ticket', {})
            if ticket.get('purpose') != 'decision':
                continue
            revision = ticket['state_revision']
            turn = turns.setdefault(revision, {'revision': revision, 'attempts': 0, 'abstentions': 0, 'proposals': {}})
            turn['attempts'] += 1
            answer = (event.get('reply') or {}).get('answers', {}).get('move', {})
            if answer.get('choice'):
                choice = answer['choice']
                turn['proposals'][choice] = turn['proposals'].get(choice, 0) + 1
            turn['abstentions'] += int(bool(answer.get('abstained')))
            if event.get('accepted_action'):
                turn['accepted_action'] = event['accepted_action']
            if event.get('rejection'):
                turn['last_rejection'] = event['rejection'][:160]
        events = list(turns.values())
        atomic_json(path, {'game_id': record['session_id'], 'completed_at': record['updated_at'],
                          'starting_player': state.starting_player, 'outcome': outcome.kind, 'winner': outcome.winner,
                          'moves': [{'player': m.player, 'action': m.action} for m in state.history],
                          'decisions': events, 'strategy_revision': record['coaching']['revision'],
                          'strategy': record['coaching']['strategy']})

    def request(self, game_id):
        ledger = self.ledger()
        consumed = set(ledger['consumed_game_ids'])
        games = [json.loads(p.read_text()) for p in (self.root / 'games').glob('*.json') if p.stem not in consumed]
        games.sort(key=lambda g: (g['game_id'] != game_id, g['completed_at'], g['game_id']))
        if not games or games[0]['game_id'] != game_id or games[0]['winner'] != 'X':
            raise ValueError('A saved, unconsumed human win is required for coaching.')
        counts = Counter('draw' if g['outcome'] == 'draw' else 'loss' if g['winner'] == 'X' else 'win' for g in games)
        return {'task': 'update', 'previous_strategy': ledger['strategy'], 'base_revision': ledger['revision'],
                'trigger_game_id': game_id, 'statistics': dict(counts), 'games': games,
                'included_game_ids': [g['game_id'] for g in games]}

    def attempt(self, game_id, details):
        atomic_json(self.root / 'attempts' / (uuid.uuid4().hex + '.json'), {'game_id': game_id, **details})

    def commit(self, request, response):
        from PyQt6.QtCore import QLockFile
        self.root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.root / 'strategy.lock'))
        if not lock.tryLock(0):
            raise ValueError('Another app is updating this strategy; retry after it finishes.')
        try:
            return self._commit(request, response)
        finally:
            lock.unlock()

    def _commit(self, request, response):
        ledger = self.ledger()
        if self.updated(request['trigger_game_id']):
            return ledger
        strategy = validate_strategy(response.get('strategy'))
        if response.get('truncated'):
            raise ValueError('Truncated coaching response; previous strategy retained.')
        if ledger['revision'] != request['base_revision']:
            raise ValueError('Strategy changed during coaching; retry with the current revision.')
        if set(response.get('included_game_ids', [])) != set(request['included_game_ids']):
            raise ValueError('Coaching did not cover the complete history window.')
        revision = {'revision': ledger['revision'] + 1, 'strategy': strategy,
                    'trigger_game_id': request['trigger_game_id'], 'request': request, 'response': response}
        ledger = {'version': 1, 'revision': revision['revision'], 'strategy': strategy,
                  'consumed_game_ids': sorted(set(ledger['consumed_game_ids']) | set(request['included_game_ids'])),
                  'revisions': ledger['revisions'] + [revision]}
        atomic_json(self.root / 'strategy.json', ledger)
        return ledger
