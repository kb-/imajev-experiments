"""Tic-tac-toe integration, assistance settings and coaching ownership."""
from app.core.game_policy import SessionPolicy
from .coaching import BASIC_QUOTED_STRATEGY, loss_context


class Coaching:
    game_id = 'tic_tac_toe'
    human_player = 'X'
    initial_strategy = BASIC_QUOTED_STRATEGY

    def storage_root(self, root):
        return root  # Preserve existing ledgers and exported session paths.

    def history(self, game, state):
        return {'starting_player': state.starting_player,
                'moves': [{'player': move.player, 'action': move.action} for move in state.history]}

    def context(self, request):
        return loss_context(request)


class Policy(SessionPolicy):
    human_player = 'X'
    prompt_choices = (('legacy', 'Original'), ('quoted', 'Quoted'), ('coached_quoted', 'Coached quoted'))
    default_prompt = 'quoted'
    coaching = Coaching()

    def initial_state(self, game, previous, alternate):
        starter = (game.computer_player if previous == game.human_player else game.human_player) if alternate else game.human_player
        return game.initial_state_for_player(starter)

    def decision(self, game, state, actions, config, strategy, attempt):
        options = {'opening_suggestion': config.opening_suggestion, 'prompt_variant': config.prompt_variant}
        if config.prompt_variant == 'coached_quoted':
            options['strategy'] = strategy.copy()
        return (game.retry_decision_request(state, actions, attempt, **options) if attempt else
                game.decision_request(state, actions, **options))

    def correct_action(self, game, state, action, config):
        return game.tactical_choice(state, action) if self.tactics_caption(config) else (action, None)

    def tactics_caption(self, config):
        return 'Immediate wins and blocks enforced' if config.tactical_guard and config.prompt_variant != 'coached_quoted' else ''

    def decision_metadata(self, game, config, revision):
        result = super().decision_metadata(game, config, revision)
        if config.prompt_variant in ('quoted', 'coached_quoted'):
            from .prompting import VERSION
            result['prompt_version'] = VERSION + ':' + config.prompt_variant
        return result

    def metadata(self, config, coached):
        return dict(super().metadata(config, coached),
                    tactical_guard=config.tactical_guard and not coached,
                    opening_suggestion=config.opening_suggestion and not coached)
