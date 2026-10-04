from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import math
import yaml


@dataclass(frozen=True)
class Config:
    game: str = 'tic_tac_toe'
    tactical_guard: bool = True
    opening_suggestion: bool = True
    prompt_variant: str = 'quoted'
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
    sections = {key: data.get(key, {}) for key in ('game', 'imajev', 'recognition', 'canvas', 'diagnostics', 'opponent')}
    if any(not isinstance(section, dict) for section in sections.values()):
        raise ValueError('Configuration sections must be mappings.')
    g, i, r, c, d, o = (sections[k] for k in sections)
    if g.get('human_symbol', 'X') != 'X':
        raise ValueError('Tic-tac-toe currently supports human X only.')
    config = Config(
        prompt_variant=o.get('prompt_variant', Config.prompt_variant),
        game=g.get('default', 'tic_tac_toe'), tactical_guard=o.get('tactical_guard', True), opening_suggestion=o.get('opening_suggestion', True), endpoint=i.get('endpoint', Config.endpoint),
        expected_model=i.get('expected_model', Config.expected_model),
        request_timeout=float(i.get('request_timeout_seconds', Config.request_timeout)),
        startup_timeout=float(i.get('startup_timeout_seconds', Config.startup_timeout)),
        threshold=float(r.get('min_effective_probability', .85)),
        observation_size=int(c.get('observation_size_pixels', 768)),
        diagnostics=d.get('enabled', False), save_sessions=d.get('save_sessions', False),
        directory=path.parent / d.get('directory', 'sessions'))
    if config.prompt_variant not in ('legacy', 'quoted'):
        raise ValueError('Move prompt must be legacy or quoted.')
    url = urlparse(config.endpoint)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.query or url.fragment:
        raise ValueError('Imajev endpoint must be an HTTP loopback URL without credentials.')
    if not all(math.isfinite(v) and v > 0 for v in (config.request_timeout, config.startup_timeout)):
        raise ValueError('Timeouts must be positive finite seconds.')
    if not math.isfinite(config.threshold) or not 0 <= config.threshold <= 1 or not 128 <= config.observation_size <= 2048:
        raise ValueError('Invalid recognition threshold or observation size.')
    if not isinstance(config.expected_model, str) or not config.expected_model or not all(isinstance(v, bool) for v in (config.diagnostics, config.save_sessions, config.tactical_guard, config.opening_suggestion)):
        raise ValueError('Invalid model or diagnostics settings.')
    return config
