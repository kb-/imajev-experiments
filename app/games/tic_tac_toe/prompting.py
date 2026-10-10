"""Reproducible prompt variants and bounded tactical facts; no minimax imports."""
from functools import lru_cache
from app.storage.strategy import validate_strategy
from app.games.tic_tac_toe.coaching import BASIC_QUOTED_STRATEGY

from app.games.tic_tac_toe.game import CELLS, WINNING_LINES, TicTacToe

VERSION = 'prompting-strategy-v1'
ARMS = ('legacy', 'raw', 'context', 'strategy', 'ids', 'consequences',
        'A', 'B', 'C', 'expanded', 'text', 'quoted')
PRIORITIES = [
    'Win immediately if possible.',
    'Otherwise prevent X from winning on its next move.',
    'Otherwise create a fork if possible.',
    'Otherwise prevent an X fork.',
    'Prefer center.',
    'Prefer the opposite corner from X.',
    'Prefer an empty corner.',
    'Prefer an empty side.',
]
QUOTED_STRATEGY = ['win immediately', 'otherwise stop X winning next turn',
                   'otherwise create a fork', 'otherwise stop an X fork',
                   'otherwise prefer center, corners, then sides']
COMPARE = ('Choose the strongest move for O. Compare the consequences of every listed move. '
           'Earlier strategy priorities override later ones.')
COORDINATES = [list(CELLS[i:i + 3]) for i in (0, 3, 6)]


def played(board, index, player):
    if board[index]:
        raise ValueError('Occupied candidate cell.')
    return board[:index] + (player,) + board[index + 1:]


@lru_cache(None)
def winner(board):
    return next((board[a] for a, b, c in WINNING_LINES
                 if board[a] and board[a] == board[b] == board[c]), None)


@lru_cache(None)
def winning_cells(board, player):
    if winner(board):
        return ()
    return tuple(i for i, mark in enumerate(board)
                 if not mark and winner(played(board, i, player)) == player)


@lru_cache(None)
def fork_cells(board, player, dangerous=False):
    """Nonterminal fork moves; dangerous excludes an opponent immediate win reply."""
    if winner(board):
        return ()
    opponent = 'X' if player == 'O' else 'O'
    cells = []
    for i, mark in enumerate(board):
        if mark:
            continue
        after = played(board, i, player)
        if (not winner(after) and len(winning_cells(after, player)) >= 2
                and (not dangerous or not winning_cells(after, opponent))):
            cells.append(i)
    return tuple(cells)


def candidate_facts(board, index, expanded=False):
    after = played(board, index, 'O')
    terminal = bool(winner(after)) or all(after)
    threats = winning_cells(board, 'X')
    remaining = () if terminal else winning_cells(after, 'X')
    position = 'center' if index == 4 else 'corner' if index in (0, 2, 6, 8) else 'side'
    facts = {'position': position, 'wins_now': winner(after) == 'O',
             'blocks_X_win_next_turn': bool(threats) and not remaining,
             'allows_X_win_next_turn': bool(remaining)}
    if expanded:
        x_forks = () if terminal else fork_cells(after, 'X', dangerous=True)
        o_wins = () if terminal else winning_cells(after, 'O')
        facts.update(creates_fork=len(o_wins) >= 2,
                     blocks_opponent_fork=bool(fork_cells(board, 'X', dangerous=True)) and not x_forks,
                     allows_opponent_fork=bool(x_forks),
                     opposite_X_corner=index in (0, 2, 6, 8) and board[8 - index] == 'X',
                     number_of_next_turn_winning_moves=len(o_wins))
    return facts


def decision_request(state, actions, arm, strategy=None):
    if arm not in ARMS:
        raise ValueError(f'Unknown prompt variant: {arm}')
    if state.next_player != 'O' or not actions:
        raise ValueError('Decision prompts require an ongoing O turn with legal actions.')
    if arm == 'legacy':
        return TicTacToe().decision_request(state, actions)
    bare = arm in ('ids', 'A', 'B', 'C', 'expanded', 'text', 'quoted')
    with_context = arm in ('context', 'A', 'B', 'C', 'expanded', 'text')
    with_strategy = arm in ('strategy', 'B', 'C', 'expanded', 'text')
    with_facts = arm in ('consequences', 'C', 'expanded', 'text', 'quoted')
    rows = [[mark or '.' for mark in state.board[i:i + 3]] for i in (0, 3, 6)]
    # Match the previous empty-strategy learning prompt exactly in the raw arm.
    context = {'game': 'tic_tac_toe', 'board': dict(zip(CELLS, state.board)),
               'current_player': state.next_player, 'starting_player': state.starting_player,
               'coordinates': 'Columns A to C left to right; rows 1 to 3 top to bottom.',
               'rules': 'Players alternate. Three matching marks in a row, column or diagonal wins. A full board without a winner is a draw.',
               'strategy': '', 'retry_attempt': 0}
    instruction = 'Choose the best legal action for O. The symbolic board is authoritative.'
    if with_context:
        context.update(you='O', opponent='X', board_rows=rows,
                       coordinates=[row.copy() for row in COORDINATES])
    if with_strategy:
        context['strategy'] = PRIORITIES.copy()
        instruction += ' Earlier strategy priorities override later ones.'
    if with_facts:
        instruction += ' Compare the consequences of every listed move.'
    if arm == 'quoted':
        context = {'game': 'tic-tac-toe', 'player_to_move': 'O', 'opponent': 'X',
                   'coordinates': [row.copy() for row in COORDINATES], 'board': rows,
                   'strategy': QUOTED_STRATEGY.copy() if strategy is None else validate_strategy(strategy)}
        instruction = COMPARE
    criteria = {}
    for action in actions:
        cell = action.id.removeprefix('place_')
        detail = candidate_facts(state.board, CELLS.index(cell), arm in ('expanded', 'text')) if with_facts else action.description
        if arm == 'text':
            # Same flattening and field order as pinned jev_api.flatten_description.
            import json
            detail = '; '.join(f'{key}: {value if isinstance(value, str) else json.dumps(value)}'
                               for key, value in detail.items())
        criteria[cell if bare else action.id] = detail
    return {'state': context, 'questions': {'move': {
        'type': 'choice', 'instructions': instruction, 'criteria': criteria}}}


def normalize_choice(choice):
    return f'place_{choice}' if choice in CELLS else choice
