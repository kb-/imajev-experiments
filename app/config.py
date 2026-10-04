from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import math
import yaml


@dataclass(frozen=True)
class Config:
    learning_enabled: bool = False
    coach_backend: str = 'shared'
    ollama_url: str = 'http://127.0.0.1:11434'
    coach_model: str = ''
    learning_directory: Path = Path('learning')
    external_inference: bool = False
    game: str = 'tic_tac_toe'
    tactical_guard: bool = True
    opening_suggestion: bool = True
    endpoint: str = 'http://127.0.0.1:8765/v1/systemone'
    expected_model: str = 'imajev-4b-nf4'
    request_timeout: float = 45
    startup_timeout: float = 300
    threshold: float = .85
    observation_size: int = 768
    diagnostics: bool = False
    save_sessions: bool = False
    directory: Path = Path('sessions')


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
    if g.get('human_symbol', 'X') != 'X':
        raise ValueError('Tic-tac-toe currently supports human X only.')
    config = Config(
        learning_enabled=l.get('enabled', False), coach_backend=l.get('coach_backend', 'shared'),
        ollama_url=l.get('ollama_url', Config.ollama_url), coach_model=l.get('model', ''),
        learning_directory=path.parent / l.get('directory', 'learning'),
        game=g.get('default', 'tic_tac_toe'), tactical_guard=o.get('tactical_guard', True), opening_suggestion=o.get('opening_suggestion', True), endpoint=i.get('endpoint', Config.endpoint),
        expected_model=i.get('expected_model', Config.expected_model),
        request_timeout=float(i.get('request_timeout_seconds', Config.request_timeout)),
        startup_timeout=float(i.get('startup_timeout_seconds', Config.startup_timeout)),
        threshold=float(r.get('min_effective_probability', .85)),
        observation_size=int(c.get('observation_size_pixels', 768)),
        diagnostics=d.get('enabled', False), save_sessions=d.get('save_sessions', False),
        directory=path.parent / d.get('directory', 'sessions'))
    url = urlparse(config.endpoint)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.query or url.fragment:
        raise ValueError('Imajev endpoint must be an HTTP loopback URL without credentials.')
    if not all(math.isfinite(v) and v > 0 for v in (config.request_timeout, config.startup_timeout)):
        raise ValueError('Timeouts must be positive finite seconds.')
    if not math.isfinite(config.threshold) or not 0 <= config.threshold <= 1 or not 128 <= config.observation_size <= 2048:
        raise ValueError('Invalid recognition threshold or observation size.')
    if not isinstance(config.expected_model, str) or not config.expected_model or not all(isinstance(v, bool) for v in (config.diagnostics, config.save_sessions, config.tactical_guard, config.opening_suggestion)):
        raise ValueError('Invalid model or diagnostics settings.')
    if not isinstance(config.learning_enabled, bool) or config.coach_backend not in ('shared', 'ollama'):
        raise ValueError('Invalid learning settings.')
    coach_url = urlparse(config.ollama_url)
    if coach_url.scheme != 'http' or coach_url.hostname not in ('localhost', '127.0.0.1', '::1') or coach_url.username or coach_url.password or coach_url.query or coach_url.fragment:
        raise ValueError('Ollama must use an HTTP loopback URL.')
    if config.coach_backend == 'ollama' and not config.coach_model:
        raise ValueError('Ollama coaching requires learning.model; install it explicitly before playing.')
    return config
