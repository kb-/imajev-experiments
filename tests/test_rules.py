from dataclasses import replace
import pytest
from app.games.tic_tac_toe.game import State, TicTacToe, WINNING_LINES

game = TicTacToe()


@pytest.mark.parametrize('line', WINNING_LINES)
def test_eight_winning_lines(line):
    board = [''] * 9
    for i in line:
        board[i] = 'X'
    assert game.outcome(State(board=tuple(board))).winner == 'X'


def test_reachable_states():
    seen = set()
    def visit(state):
        key = state.board
        if key in seen:
            return
        seen.add(key)
        xs, os = key.count('X'), key.count('O')
        assert xs in (os, os+1)
        assert state.revision == xs + os == len(state.history)
        assert state.next_player == ('X' if xs == os else 'O')
        assert game.decode_state(game.encode_state(state)) == state
        result = game.outcome(state)
        actions = game.legal_actions(state)
        if result.kind != 'ongoing':
            assert not actions
            with pytest.raises(ValueError):
                game.apply_action(state, 'place_A1')
        else:
            assert len(actions) == key.count('')
            for action in actions:
                after = game.apply_action(state, action.id)
                assert state.board == key  # Immutable original.
                visit(after)
    visit(State())
    assert len(seen) == 5478


def test_occupied_and_tampered_record():
    state = game.apply_action(State(), 'place_A1')
    with pytest.raises(ValueError):
        game.apply_action(state, 'place_A1')
    record = game.encode_state(state)
    record['next_player'] = 'X'
    with pytest.raises(ValueError):
        game.decode_state(record)


def test_win_precedes_full_board_draw():
    state = State()
    for cell in ('A1', 'B1', 'C1', 'A2', 'B2', 'C2', 'B3', 'A3', 'C3'):
        state = game.apply_action(state, f'place_{cell}')
    assert game.outcome(state).winner == 'X'


def test_draw():
    state = State()
    for cell in ('A1', 'B1', 'C1', 'B2', 'A2', 'C2', 'B3', 'A3', 'C3'):
        state = game.apply_action(state, f'place_{cell}')
    assert game.outcome(state).kind == 'draw'
