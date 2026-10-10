"""Session adjudication, independent of each game's board rules."""
from app.core.contracts import Outcome


DECISION_RETRY_LIMIT = 3
FORFEIT_MESSAGE = 'Imajev forfeits after three failed retries. You won!'
FORFEIT_GUIDANCE = (
    'A decision_retry_limit forfeit is a loss by repeated rejected computer decisions, '
    'not a board victory or proof of tactical defeat. The board may still be ongoing. '
    'Use termination and forfeit_evidence to explain the refusal or illegal answers. '
    'Do not invent a winning line, an accepted move, or an avoidable tactical mistake. '
    'Revise strategy only when the recorded board and accepted actions support a lesson; '
    'otherwise retain the useful existing strategy and state uncertainty. '
    'Preserve the forfeit cause when summarizing history. ')


def retry_progress(events, revision):
    """Recover retries at this board revision, including legacy event records."""
    failed = 0
    next_attempt = 0
    had_failure = False
    for event in events:
        ticket = event.get('ticket', {})
        if ticket.get('purpose') != 'decision' or ticket.get('state_revision') != revision:
            continue
        attempt = event.get('decision_attempt')
        if type(attempt) is not int or attempt < 0:
            attempt = next_attempt
        completed = bool(event.get('reply') or event.get('error') or event.get('rejection'))
        next_attempt = max(next_attempt, attempt + int(completed))
        if event.get('accepted_action'):
            failed, had_failure, next_attempt = 0, False, 0
        elif event.get('rejection') and event.get('reply') and not event.get('error'):
            if attempt > 0 or had_failure:
                failed += 1
            had_failure = True
        elif event.get('error'):
            had_failure = True
    return min(failed, DECISION_RETRY_LIMIT), next_attempt


def retry_forfeit(game, state):
    return {'kind': 'forfeit', 'reason': 'decision_retry_limit',
            'loser': game.computer_player, 'winner': game.human_player,
            'state_revision': game.revision(state), 'failed_retries': DECISION_RETRY_LIMIT}


def session_outcome(game, state, termination=None, events=None):
    """Adjudicate a forfeit without changing the authoritative board state."""
    outcome = game.outcome(state)
    if termination is None:
        return outcome
    expected = retry_forfeit(game, state)
    if (not isinstance(termination, dict) or termination != expected
            or any(type(termination[key]) is not type(value) for key, value in expected.items())
            or outcome.kind != 'ongoing'
            or game.current_player(state) != game.computer_player):
        raise ValueError('Forfeit metadata does not match the ongoing computer turn.')
    if events is not None and retry_progress(events, game.revision(state))[0] != DECISION_RETRY_LIMIT:
        raise ValueError('Forfeit metadata is not supported by three failed retries.')
    return Outcome('win', game.human_player)
