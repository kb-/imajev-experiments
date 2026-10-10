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
    return dict(request, games=games, winning_lines=[[CELLS[i] for i in line] for line in WINNING_LINES])
