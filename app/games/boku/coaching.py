"""Boku coach knowledge and accepted-game replay. No tic-tac-toe assumptions."""
from .game import Boku, RULES
from .geometry import COORDINATES, AXES, CELLS
from app.core.results import FORFEIT_GUIDANCE, session_outcome

BASIC_QUOTED_STRATEGY = ['win immediately', 'otherwise prevent Black winning next turn']


def coaching_context():
    common = ('You coach White (Imajev) in Boku against the human Black. '
              'Use the authoritative game_rules and axial geometry in every analysis. '
              'A turn includes placement and any mandatory capture before victory is checked. '
              'Do not apply square-grid or tic-tac-toe heuristics. '
              'Base claims on accepted actions and reconstructed positions; do not invent moves, threats or legal alternatives. ')
    return {
        'rules': RULES + ' Human is Black; Imajev is White.',
        'geometry': {'cells': COORDINATES, 'axes': AXES,
                     'description': 'Cells map to axial (q,r). Adjacent cells differ by one axis or its negative. Rows A–K run top to bottom; numbers run left to right within a row.'},
        'instructions': {
            'diagnose': common + ('Find the earliest avoidable White mistake, checking earlier turns when the last position was already lost. '
                'If the evidence does not establish an avoidable mistake, say so. Return at most 120 words in four labeled lines: '
                'Mistake: action number and action ID. Continuation: the accepted sequence and concrete winning line or capture mechanism. '
                'Alternative: a legal alternative and why it avoids that continuation, or state uncertainty. Lesson: a general condition and action.'),
            'summarize': common + ('Summarize the supplied games, ordered action segments or summaries in at most 120 words. '
                'Retain action numbers, concrete loss evidence, captures, reserve/forbidden-space changes and useful wins/draws. '
                'Segments are partial evidence; do not declare their final position a completed game.'),
            'update': common + ('Revise the previous ordered strategy using the loss diagnosis and all supplied history. '
                'Preserve useful rules and make only changes supported by evidence. Higher rules override lower rules. '
                'Rules must apply to legal placement or capture choices as appropriate and account for complete turns. '
                'Return only the complete strategy as a numbered list starting at 1, one rule per line, descending priority. '
                'Prefer 3–6 rules when sufficient; at most 12 rules and 120 words total, at most 160 characters per rule. No extra prose.')
        }}


def position(state):
    return {'stones': {c: p for c, p in zip(CELLS, state.board) if p},
            'player': state.next_player, 'phase': state.phase,
            'reserves': dict(zip(('Black', 'White'), state.reserves)),
            'forbidden': state.forbidden, 'capture_candidates': list(state.capture_candidates)}


def enrich_game(record):
    game = Boku()
    state = game.initial_state_for_player(record['starting_player'])
    actions = []
    # A checkpoint every eight actions allows bounded, independently replayable segments.
    checkpoints = []
    for index, move in enumerate(record['moves']):
        if move['player'] != state.next_player:
            raise ValueError('Boku coaching history has an invalid player sequence.')
        if index % 8 == 0:
            checkpoints.append({'action_number': index + 1, 'position': position(state)})
        before = state
        state = game.apply_action(state, move['action'])
        actions.append({'number': index + 1, **move,
                        **({'eligible_captures': list(before.capture_candidates)} if before.phase == 'capture' else {})})
    outcome = session_outcome(game, state, record.get('termination'), record.get('forfeit_evidence'))
    if outcome.kind != record['outcome'] or outcome.winner != record['winner']:
        raise ValueError('Boku coaching result does not match accepted actions.')
    segments = []
    for i, checkpoint in enumerate(checkpoints):
        start = checkpoint['action_number'] - 1
        end = min(start + 8, len(actions))
        segments.append({'game_id': record['game_id'], 'action_range': [start+1, end],
                         'start_position': checkpoint['position'], 'actions': actions[start:end],
                         'end_position': checkpoints[i+1]['position'] if i+1 < len(checkpoints) else position(state),
                         **({'termination': record['termination'], 'forfeit_evidence': record['forfeit_evidence']}
                            if record.get('termination') and i+1 == len(checkpoints) else {})})
    return dict(record, actions=actions, checkpoints=checkpoints, _segments=segments, final_position=position(state),
                result_for_computer='draw' if outcome.kind == 'draw' else
                'loss' if outcome.winner == game.human_player else 'win')


def loss_context(request):
    context = coaching_context()
    if any(g.get('termination') for g in request['games']):
        context['instructions'] = {task: FORFEIT_GUIDANCE + text for task, text in context['instructions'].items()}
    return dict(request, coach_context=context, games=[enrich_game(g) for g in request['games']])
