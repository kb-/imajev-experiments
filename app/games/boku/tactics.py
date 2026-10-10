"""One-reply tactical evidence, evaluated by the Boku rules engine.

This supplies facts, never selects or overrides an inference action.
"""
from dataclasses import replace
from functools import lru_cache

from .game import Boku

FACT_DEFAULTS = {
    'wins_now': False,
    'blocks_Black_win_next_turn': False,
    'allows_Black_win_next_turn': False,
    'allows_Black_capture_forced_win': False,
}
FACT_SCOPE = (
    'Omitted candidate fact flags are false. Facts include completed mandatory captures. '
    'Placement flags assume White chooses its best eligible capture: wins_now means a winning '
    'completion exists; allows_Black_win_next_turn means every completion allows a Black win; '
    'blocks_Black_win_next_turn means Black currently threatens a win and a safe completion exists. '
    'capture_options gives the facts for each individual capture choice. '
    'allows_Black_capture_forced_win means Black can place and capture so that every complete '
    'White reply still allows Black to win on its following turn. This tests capture setups '
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
    return tuple(action.id for action in game.legal_actions(state)
                 if any(game.outcome(end).winner == 'Black'
                        for _, end in completions(game, state, action.id)))


def candidate_facts(state, actions):
    from .capture_traps import capture_trap
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
                     'allows_Black_capture_forced_win': not win and not unsafe and
                         capture_trap(game, replace(end, history=(), revision=0)) is not None}
            branches.append(facts)
            if capture:
                captures[capture] = {key: value for key, value in facts.items() if value}
        # White controls its own capture. Preserve all branches as evidence so
        # the follow-up capture decision can realize the safe/winning branch.
        facts = {'wins_now': any(b['wins_now'] for b in branches),
                 'blocks_Black_win_next_turn': any(b['blocks_Black_win_next_turn'] for b in branches),
                 'allows_Black_win_next_turn': all(b['allows_Black_win_next_turn'] for b in branches),
                 'allows_Black_capture_forced_win':
                     any(b['allows_Black_capture_forced_win'] for b in branches) and
                     all(b['allows_Black_win_next_turn'] or b['allows_Black_capture_forced_win'] for b in branches)}
        result[action.id] = {key: value for key, value in facts.items() if value}
        if captures:
            result[action.id]['capture_options'] = captures
    return result
