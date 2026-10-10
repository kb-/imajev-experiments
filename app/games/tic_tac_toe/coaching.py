"""Factual loss context reconstructed from accepted moves, without tactical advice."""
from app.games.tic_tac_toe.game import CELLS, WINNING_LINES, TicTacToe


def enrich_game(game, include_threats=False):
    rules = TicTacToe()
    state = rules.initial_state_for_player(game['starting_player'])
    turns = []

    def rows(board):
        return [''.join(mark or '.' for mark in board[i:i + 3]) for i in (0, 3, 6)]

    for index, move in enumerate(game['moves']):
        if move['player'] != state.next_player:
            raise ValueError('Coaching history does not follow the accepted player sequence.')
        after = rules.apply_action(state, move['action'])
        if move['player'] == 'O':
            next_move = game['moves'][index + 1] if index + 1 < len(game['moves']) else None
            turns.append({'move_number': index + 1,
                          'board_before': rows(state.board),
                          'legal_cells': [a.id.removeprefix('place_') for a in rules.legal_actions(state)],
                          'chosen_cell': move['action'].removeprefix('place_'),
                          'board_after': rows(after.board),
                          'next_X_move': next_move['action'].removeprefix('place_') if next_move else None,
                          'board_after_X_reply': rows(rules.apply_action(after, next_move['action']).board)
                          if next_move else None})
            if include_threats:
                from app.games.tic_tac_toe.prompting import winning_cells
                reply_state = rules.apply_action(after, next_move['action']) if next_move else None
                turns[-1].update(
                    X_winning_cells_before_O_move=[CELLS[i] for i in winning_cells(state.board, 'X')],
                    X_winning_cells_after_O_move=[CELLS[i] for i in winning_cells(after.board, 'X')],
                    X_winning_cells_after_reply=[CELLS[i] for i in winning_cells(reply_state.board, 'X')]
                    if reply_state else [],
                    O_winning_cells_after_reply=[CELLS[i] for i in winning_cells(reply_state.board, 'O')]
                    if reply_state else [])
        state = after
    return dict(game, turns=turns)


def loss_context(request, include_threats=False):
    games = [dict(game, result_for_O=('draw' if game['outcome'] == 'draw'
                                    else 'loss' if game['winner'] == 'X' else 'win'))
             for game in request['games']]
    games[0] = enrich_game(games[0], include_threats)
    return dict(request, games=games, coach_context=coaching_context(), winning_lines=[[CELLS[i] for i in line] for line in WINNING_LINES])


BASIC_QUOTED_STRATEGY = ['win immediately', 'otherwise stop X winning next turn']


def coaching_context():
    instructions = {}
    for task in ('diagnose', 'summarize', 'update'):
        request = {'task': task}
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
        instructions[task] = instruction
    return {'instructions': instructions}
