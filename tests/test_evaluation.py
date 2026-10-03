from scripts.evaluate import oracle, o_turn_states


def test_minimax_oracle_is_separate_and_complete():
    assert oracle(('',) * 9, 'X') == 0
    assert oracle(('O', 'O', 'O', 'X', 'X', '', '', '', ''), 'X') == 1
    assert oracle(('X', 'X', 'X', 'O', 'O', '', '', '', ''), 'O') == -1
    assert len(list(o_turn_states())) == 2097
