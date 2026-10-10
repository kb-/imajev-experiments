"""Shared coach contract and bounded history reduction for either backend."""
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit
import httpx
from app.storage.coached import validate_strategy


class ContextBudget(ValueError):
    pass


def coach_messages(request):
    if request.get('task', 'update') == 'diagnose':
        instruction = ('Analyze the supplied accepted tic-tac-toe game for a later strategy coach. O is the computer, X the opponent. '
                       'Coordinates are columns A-C left to right, rows 1-3 top to bottom. Three matching marks win. '
                       'Find the earliest avoidable O mistake in the loss, using the reconstructed boards and legal alternatives. '
                       'A fork creates two distinct immediate winning moves on the next turn. Distinguish a preventable fork from a final position '
                       'where every move loses. Inspect every O turn in chronological order, using its move_number. '
                       'If X can win at two different cells, blocking only one still loses: inspect the preceding O turn. '
                       'When supplied, X_winning_cells fields are authoritative immediate threat cells computed from the rules. '
                       'Use next_X_move as the accepted reply and verify claimed threats against these fields. '
                       'Check the supplied winning lines and the empty cells before making any claim. '
                       'Return four short labeled lines, at most 120 words total: '
                       'Mistake: the earliest avoidable O move number and cell. '
                       'Continuation: the accepted X reply and the two distinct winning cells it creates, or another concrete winning mechanism. '
                       'Alternative: one legal O move at the mistake position and why it avoids that continuation. '
                       'Lesson: a general condition and action for future games. '
                       'Do not invent moves or recommend occupied cells. No commentary.')
    elif request.get('task', 'update') == 'summarize':
        instruction = ('Summarize these tic-tac-toe games or summaries for a later strategy coach. O is the computer, X its opponent. '
                       'Retain concrete move sequences and recurring loss causes, plus useful evidence from wins and draws. '
                       'Do not invent moves or claim a cause without evidence. Return only a concise summary, at most 120 words.')
    else:
        instruction = ('You coach O in tic-tac-toe against X. Columns A-C run left to right, rows 1-3 top to bottom. '
                       'Players alternate placing a mark in an empty cell; three matching marks in a row, column or diagonal wins. '
                       'A game with winner X is a loss for O; result_for_O labels the computer result. '
                       'Use the supplied winning_lines and reconstructed boards to check every claimed threat. '
                       'Revise the previous ordered strategy using the supplied games and summaries. Focus on the earliest avoidable mistake in each loss, '
                       'rather than treating the final move of an already lost position as the cause. Make only the few general rule changes supported '
                       'by the observed games. Retain useful existing rules; do not add a generic checklist or duplicate rules. '
                       'Rules have descending priority: earlier rules override later rules. You may rewrite, remove or reorder any rule. '
                       'Describe conditions and actions precisely. A fork means two distinct immediate winning moves available on the next turn. '
                       'Return only the complete revised strategy as a numbered list, one short rule per line, starting at 1. '
                       'Prefer 3-6 rules when sufficient; at most 12 rules and 120 words total, each rule at most 160 characters. '
                       'No headings, commentary or code fences.')
    # Coverage is bookkeeping, not hundreds of IDs taking away reasoning context.
    def without_coverage(value):
        if isinstance(value, dict):
            return {k: without_coverage(v) for k, v in value.items() if k not in ('included_game_ids', 'base_revision')}
        if isinstance(value, list):
            return [without_coverage(v) for v in value]
        return value
    payload = without_coverage(request)
    return [{'role': 'system', 'content': instruction},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


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
            if not (client.service_metadata or {}).get('coaching'):
                raise RuntimeError('The service does not support coaching. Restart it with the updated local launcher.')
            response = transport.post(urlunsplit((parts.scheme, parts.netloc, '/v1/coach', '', '')), json=request)
        if response.status_code == 422:
            detail = response.json().get('detail', {})
            if isinstance(detail, dict) and detail.get('code') == 'context_budget':
                raise ContextBudget('Coach input exceeds the tokenizer budget.')
        if response.status_code != 200:
            raise RuntimeError(f'Coach HTTP {response.status_code}: {response.text[:4096]}')
        return response.json()
    except httpx.TimeoutException:
        client.uncertain_busy = True
        raise


def diagnose_then_coach(request, invoke, record, byte_budget=9000, on_diagnosis=None):
    """Explain the triggering loss before revising rules; retain both stage results."""
    diagnostic = {'task': 'diagnose', 'games': [request['games'][0]],
                  'winning_lines': request['winning_lines']}
    start = time.monotonic()
    analysis = error = None
    try:
        if len(json.dumps(coach_messages(diagnostic), ensure_ascii=False).encode()) > byte_budget:
            raise ContextBudget('Triggering loss exceeds the diagnosis budget.')
        analysis = invoke(diagnostic)
        if (analysis.get('truncated') or not isinstance(analysis.get('summary'), str)
                or not analysis['summary'].strip() or len(analysis['summary'].encode()) > 2048):
            raise ValueError('Coach returned empty, oversized or truncated diagnosis; previous rules retained.')
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        stage = {'request': diagnostic, 'response': analysis, 'error': error,
                 'seconds': time.monotonic() - start}
        record(stage)
    if on_diagnosis:
        on_diagnosis(analysis)
    update = dict(request, loss_analysis=analysis['summary'],
                  revision_instruction=('Use the loss diagnosis to correct rules contradicted by this game. '
                                        'Preserve useful rules and their relative priority. '
                                        'Replace a harmful rule instead of adding generic advice.'))
    response = coach_pipeline(update, invoke, record, byte_budget)
    return dict(response, diagnosis=analysis, stages=[stage] + response['stages'])


def coach_pipeline(request, invoke, record, byte_budget=9000):
    """Cover all games; only the final validated update can be committed."""
    stages = []

    def call(payload):
        if len(json.dumps(coach_messages(payload), ensure_ascii=False).encode()) > byte_budget:
            raise ContextBudget('Coaching input needs history reduction.')
        start = time.monotonic()
        result = error = None
        try:
            result = invoke(payload)
            if result.get('truncated'):
                raise ValueError('Truncated coaching response.')
            if payload['task'] == 'update':
                validate_strategy(result.get('strategy'))
            elif not isinstance(result.get('summary'), str) or not result['summary'].strip():
                raise ValueError('Coach returned no summary.')
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            stage = {'request': payload, 'response': result, 'error': error, 'seconds': time.monotonic() - start}
            stages.append(stage)
            record(stage)

    try:
        result = call(request)
    except ContextBudget:
        # Keep the triggering loss intact in the final update.
        trigger, others = request['games'][0], request['games'][1:]
        if not others:
            raise ContextBudget('Strategy and triggering game exceed the coaching budget.')

        def summarize(items, key):
            coverage = ([g['game_id'] for g in items] if key == 'games' else
                        [game_id for item in items for game_id in item['included_game_ids']])
            payload = {'task': 'summarize', key: items, 'included_game_ids': coverage}
            try:
                return [{'text': call(payload)['summary'], 'included_game_ids': coverage}]
            except ContextBudget:
                if len(items) <= 1:
                    raise ContextBudget('One history item exceeds the coaching budget.')
                half = len(items) // 2
                return summarize(items[:half], key) + summarize(items[half:], key)

        summaries = summarize(others, 'games')
        for _ in range(16):
            final = dict(request, games=[trigger], summaries=summaries)
            try:
                result = call(final)
                break
            except ContextBudget:
                reduced = summarize(summaries, 'summaries')
                if len(json.dumps(coach_messages({'summaries': reduced})).encode()) >= len(json.dumps(coach_messages({'summaries': summaries})).encode()):
                    raise ContextBudget('History summaries did not shrink enough; previous rules retained.')
                summaries = reduced
        else:
            raise ContextBudget('History reduction did not converge.')
    return dict(result, included_game_ids=request['included_game_ids'], stages=stages)
