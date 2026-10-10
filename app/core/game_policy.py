"""Small game integration interface; lifecycle and GPU ownership stay in the app."""
from pathlib import Path
from typing import Protocol


class CoachingPolicy(Protocol):
    game_id: str
    human_player: str
    initial_strategy: list[str]
    def context(self, request: dict) -> dict: ...
    def history(self, game, state) -> dict: ...
    def storage_root(self, root: Path) -> Path: ...


class SessionPolicy:
    """Common defaults for games; subclasses own all game-specific choices."""
    prompt_choices: tuple[tuple[str, str], ...] = ()
    default_prompt = 'legacy'
    coaching: CoachingPolicy | None = None

    def validate_prompt(self, variant):
        choices = list(dict(self.prompt_choices))
        if variant not in choices:
            expected = ', '.join(choices[:-1]) + ' or ' + choices[-1] if choices else 'provided by this game'
            raise ValueError('Move prompt must be ' + expected + '.')
        return variant

    def decision(self, game, state, actions, config, strategy, attempt):
        return game.decision_request(state, actions)

    def correct_action(self, game, state, action, config):
        return action, None

    def metadata(self, config, coached):
        return {'prompt_variant': config.prompt_variant}

    def decision_metadata(self, game, config, revision):
        return {'prompt_variant': config.prompt_variant,
                'prompt_version': game.prompt_version + ':' + config.prompt_variant,
                **({'strategy_revision': revision} if config.prompt_variant == 'coached_quoted' else {})}

    def details(self, state):
        return ''

    def tactics_caption(self, config):
        return ''

    def restore_prompt(self, record):
        return self.validate_prompt(record.get('prompt_variant', 'legacy'))

    def initial_state(self, game, previous, alternate):
        return game.initial_state()
