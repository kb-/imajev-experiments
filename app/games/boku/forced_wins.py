"""Bounded Black setup → every White reply → Black win evidence."""
from dataclasses import replace

from .tactics import completions, black_winning_replies


def compact(state, **kwargs):
    return replace(state, history=(), revision=0, **kwargs)


def forced_win_setup(game, state, *, captures_only=False):
    """Return a verified setup witness; absence means only this horizon is clear."""
    if game.outcome(state).kind != 'ongoing' or state.board.count('Black') < 3:
        return None
    if state.next_player != 'Black' or state.phase != 'placement':
        raise ValueError('Expected a completed White turn.')
    for placement in game.legal_actions(state):
        pending = game.apply_action(state, placement.id)
        if captures_only and pending.phase != 'capture':
            continue
        branches = ((capture.id, game.apply_action(pending, capture.id))
                    for capture in game.legal_actions(pending)) if pending.phase == 'capture' else ((None, pending),)
        for capture, completed in branches:
            after = compact(completed)
            if game.outcome(after).kind != 'ongoing':
                continue  # Immediate wins already have their own fact.
            threats = black_winning_replies(compact(after, next_player='Black', forbidden=None))
            if not threats:
                continue
            replies = game.legal_actions(after)
            checked = 0
            for reply in replies:
                for _, end in completions(game, after, reply.id):
                    checked += 1
                    if game.outcome(end).kind != 'ongoing' or not black_winning_replies(compact(end)):
                        break  # A White win, draw or safe capture refutes the setup.
                else:
                    continue
                break
            else:
                return {'Black_placement': placement.id, 'Black_capture': capture,
                        'threatened_winning_actions': list(threats),
                        'White_forbidden': after.forbidden,
                        'White_reply_actions_checked': len(replies),
                        'White_reply_completions_checked': checked}
    return None
