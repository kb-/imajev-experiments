from dataclasses import asdict, dataclass, replace
import math
from app.core.contracts import Action, Outcome, Reply, Scene, Stroke

CELLS = tuple(f'{col}{row}' for row in range(1, 4) for col in 'ABC')
WINNING_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
PROMPT_VERSION = 'tic-tac-toe-v5'


@dataclass(frozen=True)
class Move:
    action: str
    player: str
    drawing: tuple[Stroke, ...] = ()


@dataclass(frozen=True)
class State:
    board: tuple[str, ...] = ('',) * 9
    next_player: str = 'X'
    history: tuple[Move, ...] = ()
    revision: int = 0
    starting_player: str = 'X'


def center(index: int) -> tuple[float, float]:
    return ((index % 3 + .5) / 3, (index // 3 + .5) / 3)


def geometry_matches(drawing: tuple[Stroke, ...], index: int) -> bool:
    """Length-weighted location check, never a symbol classifier."""
    x0, y0 = index % 3 / 3, index // 3 / 3
    total = inside = 0.0
    for stroke in drawing:
        if not stroke.points:
            return False
        if any(not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= 1 or not 0 <= y <= 1 for x, y in stroke.points):
            return False
        for (ax, ay), (bx, by) in zip(stroke.points, stroke.points[1:]):
            length = math.hypot(bx - ax, by - ay)
            steps = max(1, math.ceil(length / .002))
            for step in range(steps):
                t = (step + .5) / steps
                x, y = ax + t * (bx - ax), ay + t * (by - ay)
                total += length / steps
                if x0 - .02 <= x <= x0 + 1/3 + .02 and y0 - .02 <= y <= y0 + 1/3 + .02:
                    inside += length / steps
    return total > 0 and inside / total >= .95


class TicTacToe:
    id = 'tic_tac_toe'
    name = 'Tic-tac-toe'
    prompt_version = PROMPT_VERSION
    human_player = 'X'
    computer_player = 'O'
    instruction = 'Draw an X in one empty cell.'
    supports_opening_suggestion = True
    supports_prompt_variants = True

    def initial_state(self) -> State:
        return State()

    def initial_state_for_player(self, player: str) -> State:
        if player not in ('X', 'O'):
            raise ValueError('Starting player must be X or O.')
        return State(next_player=player, starting_player=player)

    def current_player(self, state: State) -> str:
        return state.next_player

    def revision(self, state: State) -> int:
        return state.revision

    def outcome(self, state: State) -> Outcome:
        for line in WINNING_LINES:
            if state.board[line[0]] and len({state.board[i] for i in line}) == 1:
                return Outcome('win', state.board[line[0]], (center(line[0]), center(line[-1])))
        return Outcome('draw' if all(state.board) else 'ongoing')

    def legal_actions(self, state: State) -> tuple[Action, ...]:
        if self.outcome(state).kind != 'ongoing':
            return ()
        return tuple(Action(f'place_{cell}', f'Place {state.next_player} in {cell}') for cell, mark in zip(CELLS, state.board) if not mark)

    def apply_action(self, state: State, action: str, drawing: tuple[Stroke, ...] = ()) -> State:
        if action not in {a.id for a in self.legal_actions(state)}:
            raise ValueError('That cell is occupied, the action is invalid, or the game has ended.')
        index = CELLS.index(action.removeprefix('place_'))
        board = list(state.board)
        board[index] = state.next_player
        return State(tuple(board), 'O' if state.next_player == 'X' else 'X',
                     state.history + (Move(action, state.next_player, drawing),), state.revision + 1, state.starting_player)

    def render(self, state: State, drawing: tuple[Stroke, ...], purpose: str) -> Scene:
        strokes: list[Stroke] = []
        circles = []
        if purpose != 'recognition':
            for move in state.history:
                index = CELLS.index(move.action.removeprefix('place_'))
                x, y = center(index)
                if move.player == 'O':
                    circles.append((x, y, .105))
                elif move.drawing:
                    strokes.extend(move.drawing)
                else:  # Rendering symbolic replay states, not a recognition fallback.
                    strokes.extend((Stroke(((x-.09, y-.09), (x+.09, y+.09))), Stroke(((x+.09, y-.09), (x-.09, y+.09)))))
        if purpose != 'decision':
            strokes.extend(drawing)
        return Scene(
            tuple((v, 0, v, 1) for v in (1/3, 2/3)) + tuple((0, v, 1, v) for v in (1/3, 2/3)),
            tuple((cell, i % 3 / 3 + .025, i // 3 / 3 + .045) for i, cell in enumerate(CELLS)),
            tuple(circles), tuple(strokes))

    def recognition_request(self, state: State, drawing: tuple[Stroke, ...]) -> dict:
        # Keep the two questions direct. Overly strict wording suppressed confidence
        # even for a clear handwritten X in the local 2B recognition pilot.
        return {
            'state': {
                'image_content': 'Hand-drawn symbol in a labelled tic-tac-toe grid.',
                'coordinates': 'Columns A, B, C from left to right; rows 1, 2, 3 from top to bottom.'},
            'questions': {
                'symbol': {
                    'type': 'choice',
                    'instructions': 'Identify the handwritten mark only, ignoring grid and printed labels.',
                    'criteria': {
                        'X': 'A handwritten letter X (two crossing diagonal strokes).',
                        'O': 'A handwritten letter O (a closed loop).',
                        'invalid': 'Blank, scribble or multiple marks.'}},
                'cell': {
                    'type': 'choice',
                    'instructions': 'Which cell contains the handwritten mark? Use invalid for blank ink, multiple marks, or a mark spanning cells.',
                    'criteria': {
                        cell: f'{("Top", "Middle", "Bottom")[i//3]} {("left", "centre", "right")[i%3]} cell: column {cell[0]}, row {cell[1]}'
                        for i, cell in enumerate(CELLS)} | {'invalid': 'Blank image, mark crosses grid boundaries, or more than one mark'}}}}

    def decode_recognition(self, state: State, drawing: tuple[Stroke, ...], reply: Reply, threshold: float) -> str:
        symbol, cell = reply.answers['symbol'], reply.answers['cell']
        if symbol.abstained:
            raise ValueError('Imajev abstained on the symbol. Undo a stroke or clear and redraw.')
        if symbol.effective_probability < threshold:
            raise ValueError(f'I could not identify one X confidently: {symbol.choice} scored {symbol.effective_probability:.1%}; {threshold:.0%} is required. Undo a stroke or clear and redraw.')
        if symbol.choice == 'O':
            raise ValueError('You are playing X. Draw a cross instead of a circle.')
        if symbol.choice != 'X':
            raise ValueError('I could not identify one X. Undo a stroke or clear and redraw.')
        if cell.abstained:
            raise ValueError('Imajev abstained on the cell. Keep one X inside a single cell and redraw.')
        if cell.effective_probability < threshold:
            raise ValueError(f'I could not identify one cell confidently: {cell.choice} scored {cell.effective_probability:.1%}; {threshold:.0%} is required. Keep one X inside a single cell.')
        if cell.choice not in CELLS:
            raise ValueError('I could not identify one cell. Keep one X inside a single cell.')
        if not geometry_matches(drawing, CELLS.index(cell.choice)):
            raise ValueError('Your ink spans cells. Keep one X inside a single cell.')
        if state.next_player != self.human_player:
            raise ValueError('Wait for your turn.')
        action = f'place_{cell.choice}'
        if action not in {a.id for a in self.legal_actions(state)}:
            raise ValueError(f'{cell.choice} is occupied. Draw in an empty cell.')
        return action

    def tactical_priorities(self, state: State) -> tuple[tuple[str, ...], str | None]:
        """One-ply wins and threats, separate from the evaluation-only minimax oracle."""
        actions = self.legal_actions(state)
        wins = tuple(a.id for a in actions if self.outcome(self.apply_action(state, a.id)).winner == 'O')
        if wins:
            return wins, 'winning move'
        x_state = replace(state, next_player='X')
        threats = tuple(a.id for a in actions if self.outcome(self.apply_action(x_state, a.id)).winner == 'X')
        if len(threats) == 1:
            return threats, 'block immediate X win'
        # Two distinct empty winning cells cannot both be blocked by one O move.
        return (), None

    def tactical_choice(self, state: State, proposed: str) -> tuple[str, str | None]:
        priorities, reason = self.tactical_priorities(state)
        if proposed in priorities or not priorities:
            return proposed, None
        return priorities[0], reason

    def decision_request(self, state: State, actions: tuple[Action, ...], opening_suggestion: bool = True, prompt_variant: str = 'legacy') -> dict:
        if prompt_variant == 'quoted':
            from .prompting import decision_request
            return decision_request(state, actions, 'quoted')
        if prompt_variant != 'legacy':
            raise ValueError('Move prompt must be legacy or quoted.')
        priorities, reason = self.tactical_priorities(state)
        x_state = replace(state, next_player='X')
        x_threats = tuple(a.id for a in actions if self.outcome(self.apply_action(x_state, a.id)).winner == 'X')
        o_wins = tuple(a.id for a in actions if self.outcome(self.apply_action(state, a.id)).winner == 'O')
        instruction = 'Choose the best legal action for O. The symbolic board is authoritative. Win immediately if possible; otherwise block any immediate X win; otherwise seek the strongest move.'
        opening = None
        if opening_suggestion and state.revision == 0 and state.next_player == 'O':
            opening = 'place_B2'
            instruction = 'Choose place_B2. O starts this game; take center B2 as the opening move. Return place_B2 as the legal action ID.'
        if opening_suggestion and state.revision == 1 and state.board.count('X') == 1 and state.board.count('O') == 0:
            x_cell = CELLS[state.board.index('X')]
            if x_cell == 'B2':
                opening = 'place_A1'
                instruction = 'Choose place_A1. X opened in center B2; O should take corner A1 to avoid a forced loss. Return place_A1 as the legal action ID.'
            else:
                opening = 'place_B2'
                instruction = f'Choose place_B2. X opened at {x_cell}; O should take the center cell B2 to avoid a forced loss. Return place_B2 as the legal action ID.'
        if len(priorities) == 1:
            cell = priorities[0].removeprefix('place_')
            instruction = f'Choose {priorities[0]}. This is the required {reason} at {cell}. Return that exact legal action ID.'
        return {'state': {'game': self.id, 'board': dict(zip(CELLS, state.board)), 'current_player': state.next_player,
                          'starting_player': state.starting_player,
                          'coordinates': 'Columns A to C left to right; rows 1 to 3 top to bottom',
                          'rules': f'{state.starting_player} starts this game. Players alternate. Three matching marks in a row, column or diagonal wins. O plays now. First win if possible; otherwise block X winning on its next move.',
                          'immediate_O_win_actions': list(o_wins), 'immediate_X_win_actions_if_unblocked': list(x_threats),
                          'opening_advice': opening},
                'questions': {'move': {'type': 'choice', 'instructions': instruction,
                                       'criteria': {a.id: a.description for a in actions}}}}

    def retry_decision_request(self, state: State, actions: tuple[Action, ...], attempt: int, opening_suggestion: bool = True, prompt_variant: str = 'legacy') -> dict:
        request = self.decision_request(state, actions, opening_suggestion, prompt_variant)
        if prompt_variant == 'quoted':
            request['state']['retry_attempt'] = attempt
            request['questions']['move']['instructions'] += (
                f' Retry {attempt}: the previous answer did not produce a move. '
                'Reconsider every listed cell and return one legal cell ID instead of abstaining.')
            return request
        opening = request['state']['opening_advice']
        priorities, _ = self.tactical_priorities(state)
        if len(priorities) == 1:
            candidate = priorities[0]
        elif opening:
            candidate = opening
        elif not opening_suggestion and state.board.count('O') == 0:
            request['state']['retry_attempt'] = attempt
            request['questions']['move']['instructions'] = (
                'Choose one legal move for O from the listed actions. The previous answer abstained; '
                'evaluate the board and return your chosen action ID.' if attempt % 2 else
                'It is O’s first turn. Select one of the legal action IDs to make a move, based on the board and rules.')
            return request
        else:
            # A fresh concrete question prevents a deterministic repeat of an abstention.
            candidate = next((a.id for a in actions if a.id == 'place_B2'), None)
            if candidate is None:
                candidate = actions[(attempt - 1) % len(actions)].id
        request['state']['retry_attempt'] = attempt
        if attempt % 2:
            request['questions']['move']['instructions'] = (
                f'Choose {candidate}. The previous request did not produce a move. '
                f'{candidate} is legal; decide now whether to play it. Return that exact action ID.')
        else:
            request['questions']['move']['instructions'] = (
                f'Play O at {candidate.removeprefix("place_")}. This is legal and advances the game. '
                f'Return {candidate} as your choice instead of abstaining.')
        return request

    def decode_decision(self, state: State, reply: Reply) -> str:
        answer = reply.answers['move']
        if answer.abstained:
            raise ValueError('Imajev abstained while choosing O. Retry will ask a different question.')
        from .prompting import normalize_choice
        choice = normalize_choice(answer.choice)
        if choice not in {a.id for a in self.legal_actions(state)}:
            raise ValueError('Imajev selected an invalid move. Retry the computer turn.')
        return choice

    def encode_state(self, state: State) -> dict:
        return {'version': 1, 'game': self.id, **asdict(state)}

    def decode_state(self, record: dict) -> State:
        if record.get('version') != 1 or record.get('game') != self.id:
            raise ValueError('Unsupported game record.')
        state = self.initial_state_for_player(record.get('starting_player', 'X'))
        try:
            for item in record['history']:
                if item['player'] != state.next_player:
                    raise ValueError('Invalid turn order.')
                strokes = tuple(Stroke(tuple(tuple(p) for p in s['points']), s['width'], s['color']) for s in item['drawing'])
                for s in strokes:
                    if not isinstance(s.color, str) or not 0 < s.width <= .1 or not s.points or any(len(p) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in p) for p in s.points):
                        raise ValueError('Invalid stroke data.')
                state = self.apply_action(state, item['action'], strokes)
            if tuple(record['board']) != state.board or record['next_player'] != state.next_player or record['revision'] != state.revision:
                raise ValueError('Board does not match history.')
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError('Malformed game record.') from exc
        return state
