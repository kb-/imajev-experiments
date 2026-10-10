"""Ordered rules and completed history, isolated from old learning experiments."""
from collections import Counter
import json
from pathlib import Path
import uuid

from app.storage.strategy import validate_strategy
from app.storage.atomic import atomic_json


class CoachedStore:
    def __init__(self, root, policy):
        self.root = Path(root)
        self.policy = policy

    def ledger(self):
        path = self.root / 'strategy.json'
        if not path.exists():
            return {'version': 1, 'game': self.policy.game_id, 'revision': 0, 'strategy': self.policy.initial_strategy.copy(),
                    'consumed_game_ids': [], 'revisions': []}
        ledger = json.loads(path.read_text())
        if ledger.get('version') != 1 or ledger.get('game', self.policy.game_id) != self.policy.game_id:
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
            if event.get('move_sampling'):
                turn['sampled_action'] = event['move_sampling']['selected_action']
            if event.get('rejection'):
                turn['last_rejection'] = event['rejection'][:160]
        events = list(turns.values())
        atomic_json(path, {'game_id': record['session_id'], 'game': game.id, 'completed_at': record['updated_at'],
                          **self.policy.history(game, state), 'outcome': outcome.kind, 'winner': outcome.winner,
                          'decisions': events, 'strategy_revision': record['coaching']['revision'],
                          'move_temperature': record.get('move_temperature', 0),
                          'strategy': record['coaching']['strategy']})

    def request(self, game_id):
        ledger = self.ledger()
        consumed = set(ledger['consumed_game_ids'])
        games = [json.loads(p.read_text()) for p in (self.root / 'games').glob('*.json') if p.stem not in consumed]
        games = [g for g in games if g.get('game', self.policy.game_id) == self.policy.game_id]
        games.sort(key=lambda g: (g['completed_at'], g['game_id']), reverse=True)
        games.sort(key=lambda g: g['game_id'] != game_id)
        if not games or games[0]['game_id'] != game_id or games[0]['winner'] != self.policy.human_player:
            raise ValueError('A saved, unconsumed human win is required for coaching.')
        counts = Counter('draw' if g['outcome'] == 'draw' else 'loss' if g['winner'] == self.policy.human_player else 'win' for g in games)
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
        ledger = {'version': 1, 'game': self.policy.game_id, 'revision': revision['revision'], 'strategy': strategy,
                  'consumed_game_ids': sorted(set(ledger['consumed_game_ids']) | set(request['included_game_ids'])),
                  'revisions': ledger['revisions'] + [revision]}
        atomic_json(self.root / 'strategy.json', ledger)
        return ledger
