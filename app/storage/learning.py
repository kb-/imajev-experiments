"""Compact learning history and a single atomic strategy/revision ledger."""
import json
import os
import uuid
from collections import Counter
from pathlib import Path


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LearningStore:
    def __init__(self, root):
        self.root = Path(root)

    def ledger(self):
        path = self.root / 'strategy.json'
        return json.loads(path.read_text()) if path.exists() else {'revision': 0, 'text': '', 'revisions': []}

    def updated(self, game_id, update_id=None):
        key = update_id or game_id
        return any(r.get('update_id', r['game_id']) == key for r in self.ledger()['revisions'])

    def save_game(self, record, game, state):
        outcome = game.outcome(state)
        events = []
        for e in record['events']:
            if e.get('ticket', {}).get('purpose') == 'startup':
                continue
            events.append({k: e[k] for k in ('player', 'accepted_action', 'forced_action', 'model_proposed_action', 'rejection', 'decision_attempt', 'strategy_revision') if k in e} |
                          {'answers': {k: {'choice': a.get('choice'), 'abstained': a.get('abstained')} for k, a in (e.get('reply') or {}).get('answers', {}).items()}})
        summary = {'game_id': record['session_id'], 'updated_at': record['updated_at'],
                   'starting_player': state.starting_player, 'outcome': outcome.kind, 'winner': outcome.winner,
                   'moves': [{'player': m.player, 'action': m.action} for m in state.history],
                   'events': events, 'strategy_revision': record['learning']['revision']}
        atomic_json(self.root / 'games' / (record['session_id'] + '.json'), summary)
        atomic_json(self.root / 'sessions' / record['session_id'] / 'session.json', record)

    def request(self, game_id, max_bytes=9000, trigger=None):
        games = [json.loads(p.read_text()) for p in (self.root / 'games').glob('*.json')]
        completed = [g for g in games if g['outcome'] != 'ongoing']
        games = [g for g in games if g['outcome'] != 'ongoing' or (trigger and g['game_id'] == game_id)]
        games.sort(key=lambda g: (g['game_id'] == game_id, g['updated_at']), reverse=True)
        if not any(g['game_id'] == game_id for g in games):
            raise ValueError('Triggering game was not saved; cannot coach.')
        counts = Counter('draw' if g['outcome'] == 'draw' else 'loss' if g['winner'] == 'X' else 'win' for g in completed)
        request = {'previous_strategy': self.ledger()['text'], 'statistics': dict(counts), 'games': []}
        if trigger:
            request['trigger'] = trigger
        # Conservative byte cap; the service also enforces the actual tokenizer budget.
        for g in games:
            candidate = dict(request, games=request['games'] + [g])
            if len(json.dumps(candidate).encode()) > max_bytes:
                if not request['games']:
                    raise ValueError('Triggering game exceeds the coaching context budget.')
                break
            request = candidate
        request['included_game_ids'] = [g['game_id'] for g in request['games']]
        return request

    def attempt(self, game_id, request, response=None, error=None, seconds=None):
        atomic_json(self.root / 'attempts' / (uuid.uuid4().hex + '.json'),
                    {'game_id': game_id, 'request': request, 'response': response, 'error': error, 'seconds': seconds})

    def commit(self, game_id, request, response):
        ledger = self.ledger()
        update_id = (request.get('trigger') or {}).get('update_id', game_id)
        if self.updated(game_id, update_id):
            return ledger
        text = response.get('strategy', '').strip()
        if not text or response.get('truncated') or len(text.encode()) > 6000:
            raise ValueError('Coach returned an empty, truncated, or oversized strategy.')
        if 'included_game_ids' in response:
            request = dict(request, included_game_ids=response['included_game_ids'],
                           games=[g for g in request['games'] if g['game_id'] in response['included_game_ids']])
        revision = {'revision': ledger['revision'] + 1, 'text': text, 'game_id': game_id, 'update_id': update_id,
                    'request': request, 'response': response}
        ledger = {'revision': revision['revision'], 'text': text, 'revisions': ledger['revisions'] + [revision]}
        atomic_json(self.root / 'strategy.json', ledger)
        return ledger
