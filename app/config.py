from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import math
import yaml


@dataclass(frozen=True)
class BackendSettings:
    backend: str = 'imajev'
    protocol: str = 'systemone'
    model: str = 'imajev-4b-nf4'
    endpoint: str = 'http://127.0.0.1:8765/v1/systemone'
    input_mode: str = 'text_image'
    generation_temperature: float = 0
    executable: str = 'llama-server'
    model_path: str = ''
    projector_path: str = ''
    runtime_args: tuple[str, ...] = ()
    max_choices: int = 255
    context_limit: int = 4096
    generation_max_tokens: int = 128


@dataclass(frozen=True)
class Config:
    opponent: BackendSettings | None = None
    coach_backend: str = 'shared'
    ollama_url: str = 'http://127.0.0.1:11434'
    coach_model: str = ''
    learning_directory: Path = Path('learning/coached-quoted')
    external_inference: bool = False
    game: str = 'tic_tac_toe'
    tactical_guard: bool = True
    opening_suggestion: bool = True
    prompt_variant: str = 'quoted'
    move_temperature: float = 0
    endpoint: str = 'http://127.0.0.1:8765/v1/systemone'
    expected_model: str = 'imajev-4b-nf4'
    request_timeout: float = 45
    startup_timeout: float = 300
    threshold: float = .85
    observation_size: int = 768
    diagnostics: bool = False
    save_sessions: bool = False
    directory: Path = Path('sessions')

    @property
    def opponent_settings(self):
        return self.opponent or BackendSettings(model=self.expected_model, endpoint=self.endpoint)

    @property
    def opponent_name(self):
        return 'Imajev' if self.opponent_settings.backend == 'imajev' else self.opponent_settings.model


def validate_loopback(endpoint, label):
    if not isinstance(endpoint, str):
        raise ValueError(f'{label} endpoint must be an HTTP loopback URL.')
    url = urlparse(endpoint)
    try:
        url.port
    except ValueError as exc:
        raise ValueError(f'Invalid {label} port.') from exc
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.query or url.fragment:
        raise ValueError(f'{label} endpoint must be an HTTP loopback URL without credentials.')
    return url


def backend_settings(section, root, recognition_model, recognition_endpoint):
    backend = section.get('backend', 'imajev')
    if backend not in ('imajev', 'ollama', 'llama_cpp', 'decider'):
        raise ValueError('Unknown opponent backend.')
    if backend == 'ollama' and section.get('protocol') not in ('chat', 'systemone'):
        raise ValueError('Ollama opponent requires explicit protocol: chat or systemone.')
    protocol = section.get('protocol', 'systemone')
    if protocol not in ('chat', 'systemone') or (backend != 'ollama' and protocol != 'systemone'):
        raise ValueError('Unsupported opponent protocol.')
    endpoint = section.get('endpoint', recognition_endpoint if backend == 'imajev' else
                           'http://127.0.0.1:11434/' + ('api/chat' if protocol == 'chat' else 'v1/systemone') if backend == 'ollama' else
                           'http://127.0.0.1:8081/v1/systemone' if backend == 'decider' else 'http://127.0.0.1:8080/v1/systemone')
    validate_loopback(endpoint, 'Opponent')
    model = section.get('model', recognition_model if backend == 'imajev' else '')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Opponent requires a nonempty model identity.')
    mode = section.get('input_mode', 'text_image' if backend == 'imajev' else 'text')
    if mode not in ('text', 'text_image') or (backend == 'decider' and mode != 'text'):
        raise ValueError('Unsupported opponent input_mode.')
    temp = validate_move_temperature(section.get('generation_temperature', 0))
    max_tokens = section.get('generation_max_tokens', 128)
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError('generation_max_tokens must be a positive integer.')
    if protocol != 'chat' and 'generation_max_tokens' in section:
        raise ValueError('generation_max_tokens applies only to chat opponents.')
    if protocol != 'chat' and temp:
        raise ValueError('generation_temperature applies only to chat opponents.')
    args = section.get('runtime_args', [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError('runtime_args must be a list of strings.')
    def local_path(key):
        value = section.get(key, '')
        if not isinstance(value, str):
            raise ValueError(f'{key} must be a path string.')
        return str((root / value).resolve()) if value else ''
    maximum = section.get('max_choices', 26 if backend == 'ollama' else 255)
    context = section.get('context_limit', 4096)
    if type(maximum) is not int or not 2 <= maximum <= 255 or type(context) is not int or context < 32:
        raise ValueError('Invalid opponent choice or context limit.')
    if backend == 'ollama' and protocol == 'systemone' and maximum > 26:
        raise ValueError('Ollama System One supports at most 26 choices.')
    executable = section.get('executable', 'llama-server')
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError('Opponent executable must be a nonempty string.')
    if '/' in executable and not Path(executable).is_absolute():
        executable = str((root / executable).resolve())
    return BackendSettings(backend, protocol, model, endpoint, mode, temp, executable,
                           local_path('model_path'), local_path('projector_path'), tuple(args), maximum, context, max_tokens)


def validate_move_temperature(value) -> float:
    message = 'Move temperature must be a finite number between 0 and 3.'
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        temperature = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(temperature) or not 0 <= temperature <= 3:
        raise ValueError(message)
    return temperature


def load_config(path: Path) -> Config:
    try:
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML configuration: {exc}') from exc
    if not isinstance(data, dict):
        raise ValueError('Configuration must be a YAML mapping.')
    sections = {key: data.get(key, {}) for key in ('game', 'imajev', 'recognition', 'canvas', 'diagnostics', 'opponent', 'learning')}
    if any(not isinstance(section, dict) for section in sections.values()):
        raise ValueError('Configuration sections must be mappings.')
    g, i, r, c, d, o, l = (sections[k] for k in sections)
    if r.get('backend', 'imajev') != 'imajev':
        raise ValueError('Drawing recognition currently supports only ImaJEV.')
    recognition_model = r.get('model', i.get('expected_model', Config.expected_model))
    recognition_endpoint = r.get('endpoint', i.get('endpoint', Config.endpoint))
    opponent = backend_settings(o, path.parent, recognition_model, recognition_endpoint)
    game_id = g.get('default', 'tic_tac_toe')
    from app.core.registry import get_game
    game = get_game(game_id)
    policy = game.session_policy
    human = game.human_player
    if g.get('human_symbol', human) != human:
        raise ValueError(f'{game_id} currently supports human {human} only.')
    config = Config(
        opponent=opponent if any(k in o for k in ('backend', 'model', 'endpoint', 'protocol', 'input_mode', 'generation_temperature', 'generation_max_tokens', 'executable', 'model_path', 'projector_path', 'runtime_args', 'max_choices', 'context_limit')) else None,
        coach_backend=l.get('coach_backend', 'shared'), ollama_url=l.get('ollama_url', Config.ollama_url),
        coach_model=l.get('model', ''), learning_directory=path.parent / l.get('directory', 'learning/coached-quoted'),
        external_inference=i.get('external_inference', False),
        prompt_variant=o.get('prompt_variant', policy.default_prompt),
        move_temperature=validate_move_temperature(o.get('move_temperature', 0)),
        game=g.get('default', 'tic_tac_toe'), tactical_guard=o.get('tactical_guard', True), opening_suggestion=o.get('opening_suggestion', True), endpoint=recognition_endpoint,
        expected_model=recognition_model,
        request_timeout=float(i.get('request_timeout_seconds', Config.request_timeout)),
        startup_timeout=float(i.get('startup_timeout_seconds', Config.startup_timeout)),
        threshold=float(r.get('min_effective_probability', .85)),
        observation_size=int(c.get('observation_size_pixels', 768)),
        diagnostics=d.get('enabled', False), save_sessions=d.get('save_sessions', False),
        directory=path.parent / d.get('directory', 'sessions'))
    policy.validate_prompt(config.prompt_variant)
    if opponent.protocol == 'chat' and config.move_temperature:
        raise ValueError('Chat opponents supply no distribution; move_temperature must be zero.')
    url = urlparse(config.endpoint)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.query or url.fragment:
        raise ValueError('Imajev endpoint must be an HTTP loopback URL without credentials.')
    if not all(math.isfinite(v) and v > 0 for v in (config.request_timeout, config.startup_timeout)):
        raise ValueError('Timeouts must be positive finite seconds.')
    if not math.isfinite(config.threshold) or not 0 <= config.threshold <= 1 or not 128 <= config.observation_size <= 2048:
        raise ValueError('Invalid recognition threshold or observation size.')
    if not isinstance(config.expected_model, str) or not config.expected_model or not all(isinstance(v, bool) for v in (config.diagnostics, config.save_sessions, config.tactical_guard, config.opening_suggestion)):
        raise ValueError('Invalid model or diagnostics settings.')
    if config.coach_backend not in ('shared', 'ollama') or not isinstance(config.external_inference, bool):
        raise ValueError('Invalid coaching backend or external inference setting.')
    coach_url = urlparse(config.ollama_url)
    if coach_url.scheme != 'http' or coach_url.hostname not in ('localhost', '127.0.0.1', '::1') or coach_url.username or coach_url.password or coach_url.query or coach_url.fragment:
        raise ValueError('Ollama must use an HTTP loopback URL.')
    if not isinstance(config.coach_model, str) or (config.coach_backend == 'ollama' and not config.coach_model.strip()):
        raise ValueError('Ollama requires learning.model; install it explicitly before playing.')
    return config
