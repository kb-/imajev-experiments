from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import os
from app.inference.imajev_client import ADAPTER_VERSION, UPSTREAM_COMMIT


class SessionStore:
    def __init__(self, root: Path, enabled: bool):
        self.root, self.enabled = root, enabled

    def write(self, session_id, record):
        if not self.enabled:
            return
        directory = self.root / session_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / 'session.tmp'
        temporary.write_text(json.dumps(record, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, directory / 'session.json')

    def image(self, ticket, png: bytes):
        if not self.enabled:
            return None
        directory = self.root / ticket.session_id
        directory.mkdir(parents=True, exist_ok=True)
        name = f'{ticket.request_id}-{ticket.purpose}.png'
        (directory / name).write_bytes(png)
        return name


def session_record(session_id, game, state, pending, events, config):
    from app.inference.opponents import role_metadata
    return {'version': 1, 'session_id': session_id, 'updated_at': datetime.now(timezone.utc).isoformat(),
            'inference': role_metadata(config),
            'upstream_commit': UPSTREAM_COMMIT, 'adapter_version': ADAPTER_VERSION,
            'expected_model': config.expected_model, 'recognition_threshold': config.threshold,
            **game.session_policy.metadata(config, config.prompt_variant == 'coached_quoted'),
            'move_temperature': config.move_temperature,
            'state': game.encode_state(state), 'pending_ink': [asdict(s) for s in pending], 'events': events}
