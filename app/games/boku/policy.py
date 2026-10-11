"""Boku prompt choices, presentation and isolated coaching strategy."""
import math

from app.core.game_policy import SessionPolicy
from .coaching import BASIC_QUOTED_STRATEGY, loss_context
from .game import RULES


class Coaching:
    game_id = 'boku'
    human_player = 'Black'
    initial_strategy = BASIC_QUOTED_STRATEGY

    def storage_root(self, root):
        return root / 'boku'

    def history(self, game, state):
        return {'starting_player': state.starting_player,
                'moves': [{'player': move.player, 'action': move.action} for move in state.history]}

    def context(self, request):
        return loss_context(request)


class Policy(SessionPolicy):
    human_player = 'Black'
    prompt_choices = (('legacy', 'Original'), ('quoted', 'Quoted'), ('coached_quoted', 'Coached quoted'))
    default_prompt = 'quoted'
    prepare_decision_in_worker = True
    coaching = Coaching()

    def decision(self, game, state, actions, config, strategy, attempt):
        options = {'prompt_variant': config.prompt_variant}
        if config.prompt_variant == 'coached_quoted':
            options['strategy'] = strategy.copy()
        return (game.retry_decision_request(state, actions, attempt, **options) if attempt else
                game.decision_request(state, actions, **options))

    def prepare_decision(self, game, state, actions, config, strategy, attempt):
        request = self.decision(game, state, actions, config, strategy, attempt)
        if not config.tactical_guard or config.prompt_variant == 'coached_quoted':
            return request, None
        if config.prompt_variant == 'legacy':
            from .tactics import candidate_facts
            facts = candidate_facts(state, actions)
            wins = [a.id for a in actions if facts[a.id].get('wins_now')]
            dangerous = lambda item: item.get('allows_Black_win_next_turn') or item.get('allows_Black_forced_win')
            defensive = ([a.id for a in actions if not dangerous(facts[a.id])]
                         if any(dangerous(f) for f in facts.values()) else [])
        else:
            wins = request['state']['immediate_White_win_actions']
            defensive = request['state']['White_defensive_actions']
        return request, {'winning_actions': wins, 'defensive_actions': defensive}

    def correct_action(self, game, state, action, config, evidence=None, probabilities=None):
        if not config.tactical_guard or config.prompt_variant == 'coached_quoted' or not evidence:
            return action, None
        wins, defensive = evidence['winning_actions'], evidence['defensive_actions']
        priorities = wins or defensive
        if not priorities or action in priorities:
            return action, None
        order = {candidate.id: index for index, candidate in enumerate(game.legal_actions(state))}
        scores = probabilities or {}
        def rank(candidate):
            score = scores.get(candidate, 0)
            return (score if isinstance(score, (int, float)) and math.isfinite(score) and score >= 0 else 0,
                    -order[candidate])
        chosen = max(priorities, key=rank)
        return chosen, ('take immediate White win' if wins else 'avoid checked Black win or forced-win setup')

    def tactics_caption(self, config):
        return ('Immediate wins and checked Boku threats enforced'
                if config.tactical_guard and config.prompt_variant != 'coached_quoted' else '')

    def explanation(self, config, mode):
        descriptions = {
            'legacy': ('Original sends the symbolic board, coordinates, phase, reserves, forbidden cell, recent turns, '
                       'rules and legal action IDs. The model is not shown the local guard analysis.'),
            'quoted': ('Quoted also sends an ordered strategy and per-action facts about immediate wins, captures, '
                       'immediate Black replies and checked forced-win setups, plus winning and defensive action lists.'),
            'coached_quoted': ('Coached quoted sends Quoted-style facts with a strategy learned from earlier losses. '
                              'The coach revises the strategy after a loss; tactical corrections are off.'),
        }
        return {
            'rules': RULES,
            'opponent_input': descriptions[mode],
            'guard': ('The app first takes a checked immediate White win. Otherwise it avoids actions that allow an '
                      'immediate Black win or a bounded forced-win setup when a checked defensive action exists. '
                      'Mandatory capture choices are checked too. This is a bounded search, not a proof of perfect play.'),
        }

    def details(self, state):
        return (f'Reserves · Black {state.reserves[0]} / White {state.reserves[1]}\n'
                + ('Gold rings: choose one stone to capture.\n' if state.phase == 'capture' else '')
                + (f'Red ring: {state.forbidden} is forbidden this turn.\n' if state.forbidden else '')
                + 'Five in a row wins. Empty reserve draws after captures and wins are resolved.')

    def metadata(self, config, coached):
        return dict(super().metadata(config, coached), game_settings_version=1)

    def restore_prompt(self, record):
        # Old Boku records retained an ignored tic-tac-toe prompt setting.
        if not record.get('game_settings_version') and 'coaching' not in record:
            return 'legacy'
        return super().restore_prompt(record)
