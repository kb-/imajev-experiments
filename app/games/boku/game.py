"""Authoritative Boku rules; inference proposes actions, never adjudicates them."""
from dataclasses import asdict, dataclass
import math
import json
from app.core.contracts import Action, Outcome, Scene, Stone, Stroke
from .geometry import AXES, CELLS, CENTERS, COORDINATES, DIRECTIONS, SPACING, geometry_matches, neighbor, recognition_view, pocket_edges

RULES = ('Black starts. Players place one stone on an empty space. Five or more adjacent stones '
         'in a straight line on any of the three hexagonal axes wins. A placement sandwiching '
         'exactly two enemy stones between friendly endpoints requires removing exactly one '
         'eligible enemy stone, even with multiple sandwiches. Return it to its owner. The opponent '
         'cannot use the removed space on their next placement. Complete capture before checking '
         'a win; otherwise an exhausted reserve or a full board without a winner draws. Each player has 36 stones.')


@dataclass(frozen=True)
class Move:
    action: str
    player: str
    drawing: tuple[Stroke, ...] = ()


@dataclass(frozen=True)
class State:
    board: tuple[str, ...] = ('',)*80
    next_player: str = 'Black'
    phase: str = 'placement'
    capture_candidates: tuple[str, ...] = ()
    forbidden: str | None = None
    reserves: tuple[int, int] = (36, 36)
    history: tuple[Move, ...] = ()
    revision: int = 0
    starting_player: str = 'Black'


class Boku:
    id = 'boku'
    name = 'Boku'
    human_player = 'Black'
    computer_player = 'White'
    prompt_version = 'boku-v5-forced-win-defence'
    instruction = 'Draw a circle in one empty pocket, then Submit.'

    @property
    def session_policy(self):
        from .policy import Policy
        return Policy()

    def initial_state(self):
        return State()

    def initial_state_for_player(self, player):
        if player != 'Black':
            raise ValueError('Black always starts Boku.')
        return State()

    def current_player(self, state):
        return state.next_player

    def revision(self, state):
        return state.revision

    def instruction_for(self, state):
        return ('Draw an X over one highlighted white stone, then Submit to capture it.'
                if state.phase == 'capture' else self.instruction)

    def outcome(self, state):
        if state.phase == 'capture':
            return Outcome()
        board = dict(zip(CELLS, state.board))
        for cell, player in board.items():
            if not player:
                continue
            for direction in AXES:
                line = [cell]
                while (n := neighbor(line[-1], direction)) and board[n] == player:
                    line.append(n)
                if len(line) >= 5:
                    return Outcome('win', player, (CENTERS[line[0]], CENTERS[line[-1]]))
        return Outcome('draw' if 0 in state.reserves or all(state.board) else 'ongoing')

    def legal_actions(self, state):
        if self.outcome(state).kind != 'ongoing':
            return ()
        if state.phase == 'capture':
            return tuple(Action(f'capture_{c}', f'Remove enemy stone at {c}') for c in state.capture_candidates)
        return tuple(Action(f'place_{c}', f'Place {state.next_player} at {c}')
                     for c, mark in zip(CELLS, state.board) if not mark and c != state.forbidden)

    def apply_action(self, state, action, drawing=()):
        if action not in {a.id for a in self.legal_actions(state)}:
            raise ValueError('Illegal Boku action: occupied, forbidden, ineligible capture, or game ended.')
        player = state.next_player
        opponent = 'White' if player == 'Black' else 'Black'
        board = dict(zip(CELLS, state.board))
        reserves = list(state.reserves)
        _, cell = action.split('_', 1)
        candidates = set()
        if state.phase == 'capture':
            board[cell] = ''
            reserves[0 if opponent == 'Black' else 1] += 1
            forbidden = cell
        else:
            board[cell] = player
            reserves[0 if player == 'Black' else 1] -= 1
            forbidden = None
            for direction in DIRECTIONS:
                a, b, end = (neighbor(cell, direction, i) for i in (1, 2, 3))
                if end and board[a] == board[b] == opponent and board[end] == player:
                    candidates.update((a, b))
        return State(tuple(board[c] for c in CELLS), player if candidates else opponent,
                     'capture' if candidates else 'placement', tuple(c for c in CELLS if c in candidates),
                     forbidden, tuple(reserves), state.history+(Move(action, player, drawing),), state.revision+1)

    def render(self, state, drawing, purpose):
        if purpose == 'recognition':
            left, top, size = recognition_view(drawing)
            centers = {c: ((x-left)/size, (y-top)/size) for c, (x, y) in CENTERS.items()
                       if left <= x <= left+size and top <= y <= top+size}
            strokes = tuple(Stroke(tuple(((x-left)/size, (y-top)/size) for x, y in s.points),
                                   s.width/size, s.color) for s in drawing)
            return Scene(labels=tuple((c, x-.018/size, y-.03/size) for c, (x, y) in centers.items()),
                         strokes=strokes,
                         lines=pocket_edges(centers, .42*SPACING/size*1.37),
                         label_color='#58635c', line_width=.0015, protect_labels=True)
        stones = tuple(
            Stone(*CENTERS[c], .35*SPACING, player,
                  ('#777d85', '#30343b', '#080b10', '#10141a') if player == 'Black' else
                  ('#ffffff', '#f6f3eb', '#b7b2a6', '#a39c8e')) for c, player in zip(CELLS, state.board) if player)
        return Scene(background='#e7cfa5', labels=tuple((c, x-.018, y+.057) for c, (x, y) in CENTERS.items()),
                     strokes=drawing if purpose != 'decision' else (), stones=stones,
                     pockets=tuple((*CENTERS[c], .42*SPACING) for c in CELLS),
                     highlights=tuple((*CENTERS[c], .43*SPACING, '#c28b35') for c in state.capture_candidates)
                     + (((*CENTERS[state.forbidden], .43*SPACING, '#b64b4b'),) if state.forbidden else ()))

    def recognition_request(self, state, drawing):
        # Recognition only classifies ink; board/phase legality is checked below.
        # A short visual question avoids distracting this head with game rules.
        return {'state': {'image_content': 'A handwritten mark in a labelled hexagonal grid.'},
                'questions': {
                    'symbol': {'type': 'choice', 'instructions': 'Identify the structure of the handwritten ink, ignoring the printed grid and labels.',
                               'criteria': {'O': 'Exactly one closed handwritten loop enclosing exactly one empty area. Uneven or angular loops count.',
                                            'X': 'Exactly two handwritten diagonal lines crossing once.',
                                            'invalid': 'Blank, a dot, an open line, scribble, or multiple loops or marks. Two circles are invalid even in the same cell.'}},
                    'cell': {'type': 'choice', 'instructions': 'Read the ID of the cell containing the handwritten symbol.',
                             'criteria': {c: f'Pocket {c}' for c in CELLS}
                             | {'invalid': 'Blank, multiple marks or mark spanning pockets.'}}}}

    def decode_recognition(self, state, drawing, reply, threshold):
        if state.next_player != self.human_player:
            raise ValueError('Wait for your turn.')
        expected = 'X' if state.phase == 'capture' else 'O'
        for key in ('symbol', 'cell'):
            answer = reply.answers[key]
            if answer.abstained or answer.effective_probability < threshold:
                raise ValueError(f'I could not identify the {key} confidently. Undo a stroke or clear and redraw.')
        if reply.answers['symbol'].choice != expected:
            raise ValueError('Draw an X to capture a stone.' if expected == 'X' else 'Draw a circle to place a stone.')
        cell = reply.answers['cell'].choice
        if cell not in CELLS or not geometry_matches(drawing, cell):
            raise ValueError('Keep one mark inside a single pocket. Undo or clear and redraw.')
        action = f'{"capture" if state.phase == "capture" else "place"}_{cell}'
        if action not in {a.id for a in self.legal_actions(state)}:
            raise ValueError('Select a highlighted enemy stone.' if state.phase == 'capture' else 'That pocket is occupied or forbidden. Draw in an empty legal pocket.')
        return action

    def decision_request(self, state, actions, prompt_variant='legacy', strategy=None):
        turns = []
        for move in state.history:
            if move.action.startswith('place_'):
                turns.append({'player': move.player, 'place': move.action[6:]})
            elif turns:
                turns[-1]['capture'] = move.action[8:]
        pending_placement = turns[-1]['place'] if state.phase == 'capture' and turns else None
        completed_turns = turns[:-1] if pending_placement else turns
        request = {'state': {'game': self.id, 'board': dict(zip(CELLS, state.board)),
                          'coordinates': COORDINATES, 'current_player': state.next_player,
                          'phase': state.phase, 'reserves': dict(zip(('Black', 'White'), state.reserves)),
                          'forbidden': state.forbidden, 'rules': RULES,
                          'pending_placement': pending_placement, 'recent_turns': completed_turns[-12:]},
                'questions': {'move': {'type': 'choice',
                    'instructions': ('Choose exactly one eligible enemy stone to remove for White.' if state.phase == 'capture'
                                     else 'Choose the strongest legal placement for White. The symbolic board is authoritative.'),
                    'criteria': {a.id: a.description for a in actions}}}}
        self.session_policy.validate_prompt(prompt_variant)
        if prompt_variant in ('quoted', 'coached_quoted'):
            from .coaching import BASIC_QUOTED_STRATEGY
            from .tactics import candidate_facts, FACT_DEFAULTS, FACT_SCOPE
            from app.storage.strategy import validate_strategy
            request['state']['strategy'] = validate_strategy(
                strategy if prompt_variant == 'coached_quoted' and strategy is not None else BASIC_QUOTED_STRATEGY)
            request['state']['candidate_fact_defaults'] = FACT_DEFAULTS.copy()
            request['state']['candidate_fact_scope'] = FACT_SCOPE
            request['questions']['move']['criteria'] = candidate_facts(state, actions)
            request['state']['immediate_White_win_actions'] = sorted(
                action for action, facts in request['questions']['move']['criteria'].items()
                if facts.get('wins_now', False))
            facts = request['questions']['move']['criteria']
            def dangerous(f):
                return f.get('allows_Black_win_next_turn', False) or f.get('allows_Black_forced_win', False)
            request['state']['White_defensive_actions'] = (
                sorted(action for action, f in facts.items() if not dangerous(f))
                if any(dangerous(f) for f in facts.values()) else [])
            request['questions']['move']['instructions'] += (
                ' Compare the consequences of each legal action through completion of any mandatory capture. '
                'Follow the ordered strategy; earlier rules override later rules. '
                'If immediate_White_win_actions is nonempty, choose one action from that list before considering any other action. '
                'Otherwise, if White_defensive_actions is nonempty, choose one action from that list. '
                'These defensive actions avoid the checked immediate losses and forced-win setups; they do not guarantee safety beyond this horizon.')
        return request

    def retry_decision_request(self, state, actions, attempt, prompt_variant='legacy', strategy=None):
        request = self.decision_request(state, actions, prompt_variant, strategy)
        request['questions']['move']['instructions'] += f' Retry {attempt}: reconsider the listed legal actions and return one exact action ID instead of abstaining.'
        return request

    def decode_decision(self, state, reply):
        answer = reply.answers['move']
        if answer.abstained:
            raise ValueError('Imajev abstained. Retry to reconsider this turn.')
        if answer.choice not in {a.id for a in self.legal_actions(state)}:
            raise ValueError('Imajev selected an illegal Boku action. Retry.')
        return answer.choice

    def encode_state(self, state):
        return {'version': 1, 'game': self.id, **asdict(state)}

    def decode_state(self, record):
        if record.get('version') != 1 or record.get('game') != self.id:
            raise ValueError('Unsupported Boku record.')
        state = State()
        try:
            for item in record['history']:
                if item['player'] != state.next_player:
                    raise ValueError('Invalid turn order.')
                strokes = tuple(Stroke(tuple(tuple(p) for p in s['points']), s['width'], s['color']) for s in item['drawing'])
                for s in strokes:
                    if not isinstance(s.color, str) or not isinstance(s.width, (int, float)) or not math.isfinite(s.width) or not 0 < s.width <= .1 or not s.points or any(len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in p) for p in s.points):
                        raise ValueError('Invalid stroke data.')
                state = self.apply_action(state, item['action'], strokes)
            canonical = self.encode_state(state)
            # JSON-normalize tuples so both in-memory and disk records work.
            if json.dumps(record, sort_keys=True, allow_nan=False) != json.dumps(canonical, sort_keys=True, allow_nan=False):
                raise ValueError('Boku state does not match accepted history.')
        except (KeyError, TypeError, IndexError, AttributeError) as exc:
            raise ValueError('Malformed Boku record.') from exc
        return state
