"""Shared coach contract and bounded history reduction for either backend."""
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit
import httpx
from app.storage.strategy import validate_strategy


class ContextBudget(ValueError):
    pass


def coach_messages(request):
    """Messages prepared from a game's explicit coaching context."""
    context = request.get('coach_context', {})
    task = request.get('task', 'update')
    instruction = context.get('instructions', {}).get(task,
        'Review the supplied game history. Return a concise summary.' if task != 'update' else
        'Revise the ordered strategy. Return only a numbered list of rules, highest priority first.')
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()
                    if k not in ('included_game_ids', 'base_revision', 'coach_context', '_segments')}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    payload = clean(request)
    if context.get('rules') is not None:
        payload['game_rules'] = context['rules']
    if context.get('geometry') is not None:
        payload['geometry'] = context['geometry']
    return [{'role': 'system', 'content': instruction},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}]


def parse_coach_output(text, task, truncated=False):
    if truncated or not isinstance(text, str) or not text.strip():
        raise ValueError('Coach returned empty or truncated output; previous rules retained. Output: ' + str(text)[:2048])
    if task in ('summarize', 'diagnose'):
        if len(text.encode()) > 2048:
            raise ValueError('Coach summary is oversized.')
        return {'summary': text.strip(), 'truncated': False}
    # Normalize presentation only: never repair JSON or discard surrounding prose.
    clean = text.strip()
    fence = re.fullmatch(r'```(?:json|text)?[ \t]*\r?\n(.*?)\r?\n```', clean, re.DOTALL | re.IGNORECASE)
    if fence:
        clean = fence.group(1).strip()
    else:
        clean = re.sub(r'^`{1,3}(?!`)', '', clean)
        clean = re.sub(r'(?<!`)`{1,3}$', '', clean).strip()
    try:
        raw = json.loads(clean)
    except ValueError as exc:
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        numbered = [re.fullmatch(r'(\d{1,2})[.)]\s+(.+)', line) for line in lines]
        if not numbered or not all(numbered) or [int(m[1]) for m in numbered] != list(range(1, len(lines) + 1)):
            raise ValueError('Coach must return numbered rules or a strategy JSON object, without extra prose. Output: ' + text[:2048]) from exc
        raw = {'strategy': [m[2] for m in numbered]}
    if not isinstance(raw, dict) or set(raw) != {'strategy'}:
        raise ValueError('Coach must return only the strategy object. Output: ' + text[:2048])
    try:
        strategy = validate_strategy(raw['strategy'])
    except ValueError as exc:
        raise ValueError(str(exc) + ' Output: ' + text[:2048]) from exc
    return {'strategy': strategy, 'truncated': False}


def shared_coach(client, request):
    parts = urlsplit(client.config.endpoint)
    try:
        with httpx.Client(timeout=300, trust_env=False) as transport:
            client._check_busy(transport)
            if not (client.service_metadata or {}).get('coaching') or (client.service_metadata or {}).get('coach_protocol') != 2:
                raise RuntimeError('The service does not support coaching. Restart it with the updated local launcher.')
            response = transport.post(urlunsplit((parts.scheme, parts.netloc, '/v1/coach', '', '')), json={'messages': coach_messages(request)})
        if response.status_code == 422:
            detail = response.json().get('detail', {})
            if isinstance(detail, dict) and detail.get('code') == 'context_budget':
                raise ContextBudget('Coach input exceeds the tokenizer budget.')
        if response.status_code != 200:
            raise RuntimeError(f'Coach HTTP {response.status_code}: {response.text[:4096]}')
        raw = response.json()
        return dict(raw, **parse_coach_output(raw.get('response_text'), request.get('task', 'update'), raw.get('truncated', False)))
    except httpx.TimeoutException:
        client.uncertain_busy = True
        raise


def prepared_transport(transport, progress=lambda message: None):
    """Adapt a raw chat transport to the validated coaching-stage contract."""
    def invoke(payload):
        task = payload.get('task', 'update')
        progress('Diagnosing loss…' if task == 'diagnose' else
                 'Studying games…' if task == 'summarize' else 'Updating strategy…')
        raw = transport({'messages': coach_messages(payload)})
        return dict(raw, **parse_coach_output(raw.get('response_text'), task, raw.get('truncated', False)))
    return invoke


class _History:
    """Budgeted requests and complete-coverage reduction, independent of game rules."""
    def __init__(self, request, invoke, record, budget):
        self.request, self.invoke, self.record, self.budget = request, invoke, record, budget
        self.stages = []

    def size(self, payload):
        return len(json.dumps(coach_messages(payload), ensure_ascii=False).encode())

    def call(self, payload):
        start = time.monotonic()
        result = error = None
        try:
            if self.size(payload) > self.budget:
                raise ContextBudget('Coaching input needs history reduction.')
            result = self.invoke(payload)
            if result.get('truncated'):
                raise ValueError('Truncated coaching diagnosis/summary/strategy response; previous rules retained.')
            if payload['task'] == 'update':
                validate_strategy(result.get('strategy'))
            elif not isinstance(result.get('summary'), str) or not result['summary'].strip() or len(result['summary'].encode()) > 2048:
                raise ValueError('Coach returned empty, oversized or truncated diagnosis/summary; previous rules retained.')
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            stage = {'request': payload, 'response': result, 'error': error, 'seconds': time.monotonic()-start}
            self.stages.append(stage)
            self.record(stage)

    def summarize(self, items, key):
        if not items:
            return []
        coverage = list(dict.fromkeys(game_id for item in items for game_id in
                       (item.get('included_game_ids', []) if key == 'summaries' else [item['game_id']])))
        payload = {'task': 'summarize', key: items, 'included_game_ids': coverage,
                   'coach_context': self.request.get('coach_context', {})}
        try:
            forfeits = {entry['game_id']: entry for item in items for entry in
                        ([{'game_id': item['game_id'], 'termination': item['termination']}]
                         if item.get('termination') else item.get('forfeits', []))}
            return [{'text': self.call(payload)['summary'], 'included_game_ids': coverage,
                     **({'forfeits': list(forfeits.values())} if forfeits else {})}]
        except ContextBudget:
            if len(items) == 1:
                if key == 'games' and items[0].get('_segments'):
                    return self.summarize(items[0]['_segments'], 'segments')
                raise ContextBudget('One history segment plus mandatory game rules exceeds the coaching budget.')
            half = len(items)//2
            return self.summarize(items[:half], key) + self.summarize(items[half:], key)

    def reduce_game(self, game):
        if not game.get('_segments'):
            raise ContextBudget('Triggering game and mandatory rules exceed the coaching budget.')
        summaries = self.summarize(game['_segments'], 'segments')
        # Summaries are ordered and all action segments are covered; keep the terminal board exact.
        return {k: game[k] for k in ('game_id', 'outcome', 'winner', 'starting_player',
                                     'final_position', 'result_for_computer', 'termination',
                                     'forfeit_evidence') if k in game} | {'evidence': summaries}

    def update(self, request):
        try:
            return self.call(request)
        except ContextBudget:
            trigger, others = request['games'][0], request['games'][1:]
            summaries = self.summarize(others, 'games')
            # Preserve small triggering games verbatim; segment only games that cannot fit.
            final = dict(request, games=[trigger], summaries=summaries)
            for _ in range(16):
                try:
                    return self.call(final)
                except ContextBudget:
                    if trigger.get('_segments'):
                        trigger = self.reduce_game(trigger)
                        final = dict(final, games=[trigger])
                        continue
                    combined = summaries or trigger.get('evidence', [])
                    if not combined:
                        raise ContextBudget('Strategy and triggering game exceed the coaching budget.')
                    reduced = self.summarize(combined, 'summaries')
                    if len(json.dumps(reduced)) >= len(json.dumps(combined)):
                        raise ContextBudget('History summaries did not shrink enough; previous rules retained.')
                    if summaries:
                        summaries = reduced
                        final = dict(final, summaries=summaries)
                    else:
                        trigger = dict(trigger, evidence=reduced)
                        final = dict(final, games=[trigger])
            raise ContextBudget('History reduction did not converge.')


def diagnose_then_coach(request, invoke, record, byte_budget=14000, on_diagnosis=None):
    """Diagnose the triggering loss, then revise rules without losing history coverage."""
    history = _History(request, invoke, record, byte_budget)
    diagnostic = dict(request, task='diagnose', games=[request['games'][0]])
    try:
        analysis = history.call(diagnostic)
    except ContextBudget:
        trigger = history.reduce_game(request['games'][0])
        diagnostic = dict(diagnostic, games=[trigger])
        # Repeated reduction is bounded and preserves the exact terminal position.
        for _ in range(16):
            try:
                analysis = history.call(diagnostic)
                break
            except ContextBudget:
                reduced = history.summarize(trigger['evidence'], 'summaries')
                if len(json.dumps(reduced)) >= len(json.dumps(trigger['evidence'])):
                    raise ContextBudget('Loss summaries did not shrink enough; previous rules retained.')
                trigger = dict(trigger, evidence=reduced)
                diagnostic = dict(diagnostic, games=[trigger])
        else:
            raise ContextBudget('Loss reduction did not converge.')
        request = dict(request, games=[trigger] + request['games'][1:])
    if on_diagnosis:
        on_diagnosis(analysis)
    update = dict(request, task='update', loss_analysis=analysis['summary'],
                  revision_instruction=('Use the loss diagnosis to correct rules contradicted by this game. '
                                        'Preserve useful rules and their relative priority. '
                                        'Replace a harmful rule instead of adding generic advice.'))
    response = history.update(update)
    return dict(response, included_game_ids=request['included_game_ids'], diagnosis=analysis, stages=history.stages)


def coach_pipeline(request, invoke, record, byte_budget=14000):
    history = _History(request, invoke, record, byte_budget)
    result = history.update(request)
    return dict(result, included_game_ids=request['included_game_ids'], stages=history.stages)
