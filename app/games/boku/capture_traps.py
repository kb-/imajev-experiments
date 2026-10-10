"""Bounded, capture-aware forced-win evidence owned by Boku."""
from dataclasses import replace

from .tactics import completions, black_winning_replies


def compact(state, **kwargs):
    return replace(state, history=(), revision=0, **kwargs)


def capture_trap(game, state):
    """Return one verified Black setup witness, or None if none exists."""
    if game.outcome(state).kind != 'ongoing':
        return None
    if state.board.count('Black') < 3:
        return None  # Setup and winning placement can add at most two stones.
    if state.next_player != 'Black' or state.phase != 'placement':
        raise ValueError('Expected a completed White turn.')
    for placement in game.legal_actions(state):
        pending = game.apply_action(state, placement.id)
        if pending.phase != 'capture':
            continue
        for capture in game.legal_actions(pending):
            after = compact(game.apply_action(pending, capture.id))
            if game.outcome(after).kind != 'ongoing':
                continue  # Immediate wins already have their own existing fact.
            threats = black_winning_replies(compact(after, next_player='Black', forbidden=None))
            if not threats:
                continue
            replies = game.legal_actions(after)
            checked = 0
            for reply in replies:
                for _, end in completions(game, after, reply.id):
                    checked += 1
                    if game.outcome(end).kind != 'ongoing' or not black_winning_replies(compact(end)):
                        break  # A White win, draw or safe continuation refutes the trap.
                else:
                    continue
                break
            else:
                return {'Black_placement': placement.id, 'Black_capture': capture.id,
                        'threatened_winning_actions': list(threats),
                        'White_forbidden': after.forbidden,
                        'White_reply_actions_checked': len(replies),
                        'White_reply_completions_checked': checked}
    return None


def evaluate(game, state, action):
    branches = []
    for capture, end in completions(game, compact(state), action):
        witness = capture_trap(game, compact(end))
        branches.append({'White_capture': capture, 'witness': witness})
    return {'allows_Black_capture_forced_win': all(b['witness'] is not None for b in branches),
            'branches': branches}


