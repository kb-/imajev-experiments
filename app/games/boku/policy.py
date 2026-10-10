"""Boku prompt choices, presentation and isolated coaching strategy."""
from app.core.game_policy import SessionPolicy
from .coaching import BASIC_QUOTED_STRATEGY, loss_context


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
    coaching = Coaching()

    def decision(self, game, state, actions, config, strategy, attempt):
        options = {'prompt_variant': config.prompt_variant}
        if config.prompt_variant == 'coached_quoted':
            options['strategy'] = strategy.copy()
        return (game.retry_decision_request(state, actions, attempt, **options) if attempt else
                game.decision_request(state, actions, **options))

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
