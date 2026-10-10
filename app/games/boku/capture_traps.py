"""Bounded, capture-aware forced-win evidence owned by Boku."""
from .tactics import completions
from .forced_wins import compact, forced_win_setup


def capture_trap(game, state):
    """Historical capture-only experiment; production also checks ordinary setups."""
    return forced_win_setup(game, state, captures_only=True)


def evaluate(game, state, action):
    branches = []
    for capture, end in completions(game, compact(state), action):
        witness = capture_trap(game, compact(end))
        branches.append({'White_capture': capture, 'witness': witness})
    return {'allows_Black_capture_forced_win': all(b['witness'] is not None for b in branches),
            'branches': branches}


