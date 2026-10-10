"""One-reply tactical evidence, evaluated by the Boku rules engine.

This supplies facts, never selects or overrides an inference action.
"""
from dataclasses import replace
from functools import lru_cache

from .game import Boku
from .geometry import AXES, CELLS, neighbor

# A winning placement must fill the sole empty cell of a five-cell window.
# Capturing removes only the opponent's stones, so it cannot create another
# friendly stone in that window. Final legality and outcomes still use Boku.
_CELL_BITS = {cell: 1 << i for i, cell in enumerate(CELLS)}
_WIN_MASKS = tuple(sum(_CELL_BITS[c] for c in line)
                  for cell in CELLS for axis in AXES
                  if all(line := tuple(neighbor(cell, axis, i) for i in range(5))))


def winning_placement_cells(board, player='Black'):
    friendly = sum(1 << i for i, mark in enumerate(board) if mark == player)
    occupied = sum(1 << i for i, mark in enumerate(board) if mark)
    candidates = 0
    for mask in _WIN_MASKS:
        if (mask & friendly).bit_count() == 4 and (mask & occupied).bit_count() == 4:
            candidates |= mask & ~occupied
    return {cell for cell, bit in _CELL_BITS.items() if candidates & bit}

FACT_DEFAULTS = {
    'wins_now': False,
    'blocks_Black_win_next_turn': False,
    'allows_Black_win_next_turn': False,
    'allows_Black_forced_win': False,
}
FACT_SCOPE = (
    'Omitted candidate fact flags are false. Facts include completed mandatory captures. '
    'Placement flags assume White chooses its best eligible capture: wins_now means a winning '
    'completion exists; allows_Black_win_next_turn means every completion allows a Black win; '
    'blocks_Black_win_next_turn means Black currently threatens a win and a safe completion exists. '
    'capture_options gives the facts for each individual capture choice. '
    'allows_Black_forced_win means Black can complete a placement, including any mandatory capture, so that every complete '
    'White reply still allows Black to win on its following turn. This tests setups with or without capture '
    'only when the completion is neither a White win nor an immediate Black danger. '
    'These are bounded tactical facts, not general playing strength.'
)


def completions(game, state, action):
    """Complete exactly this turn, including one compulsory removal if needed."""
    after = game.apply_action(state, action)
    if after.phase == 'capture':
        return tuple((capture.id, game.apply_action(after, capture.id))
                     for capture in game.legal_actions(after))
    return ((None, after),)


@lru_cache(maxsize=256)
def black_winning_replies(state):
    game = Boku()
    if game.outcome(state).kind != 'ongoing' or state.board.count('Black') < 4:
        return ()
    if state.next_player != 'Black' or state.phase != 'placement':
        raise ValueError('Black reply evidence requires a completed White turn.')
    candidates = winning_placement_cells(state.board)
    return tuple(action.id for action in game.legal_actions(state)
                 if action.id[6:] in candidates
                 if any(game.outcome(end).winner == 'Black'
                        for _, end in completions(game, state, action.id)))


def candidate_facts(state, actions):
    from .forced_wins import forced_win_setup
    game = Boku()
    if state.next_player != 'White' or game.outcome(state).kind != 'ongoing':
        raise ValueError('Boku tactical facts require an ongoing White turn.')
    # Do not retain handwriting or whole histories in the bounded reply cache.
    state = replace(state, history=(), revision=0)
    # Baseline: threats if Black could place on this board. An old prohibition
    # applies to White's current turn, not Black's following placement.
    baseline = replace(state, next_player='Black', phase='placement',
                       capture_candidates=(), forbidden=None)
    threatened = bool(black_winning_replies(baseline))
    result = {}
    for action in actions:
        branches = []
        captures = {}
        for capture, end in completions(game, state, action.id):
            win = game.outcome(end).winner == 'White'
            unsafe = game.outcome(end).winner == 'Black' or bool(
                black_winning_replies(replace(end, history=(), revision=0)))
            facts = {'wins_now': win,
                     'blocks_Black_win_next_turn': threatened and not unsafe,
                     'allows_Black_win_next_turn': unsafe,
                     'allows_Black_forced_win': not win and not unsafe and
                         forced_win_setup(game, replace(end, history=(), revision=0)) is not None}
            branches.append(facts)
            if capture:
                captures[capture] = {key: value for key, value in facts.items() if value}
        # White controls its own capture. Preserve all branches as evidence so
        # the follow-up capture decision can realize the safe/winning branch.
        facts = {'wins_now': any(b['wins_now'] for b in branches),
                 'blocks_Black_win_next_turn': any(b['blocks_Black_win_next_turn'] for b in branches),
                 'allows_Black_win_next_turn': all(b['allows_Black_win_next_turn'] for b in branches),
                 'allows_Black_forced_win':
                     any(b['allows_Black_forced_win'] for b in branches) and
                     all(b['allows_Black_win_next_turn'] or b['allows_Black_forced_win'] for b in branches)}
        result[action.id] = {key: value for key, value in facts.items() if value}
        if captures:
            result[action.id]['capture_options'] = captures
    return result
