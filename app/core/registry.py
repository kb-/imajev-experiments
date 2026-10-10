from app.core.contracts import Game
from app.games.tic_tac_toe.game import TicTacToe
from app.games.boku.game import Boku

GAMES: dict[str, Game] = {'tic_tac_toe': TicTacToe(), 'boku': Boku()}


def get_game(game_id: str) -> Game:
    if game_id not in GAMES:
        raise ValueError(f'Unknown game: {game_id}')
    return GAMES[game_id]
