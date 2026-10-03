"""Add a busy gate and provenance to the pinned upstream image service.

The upstream model loader, choice endpoint and CLI are used unchanged.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid


class BusyGuard:
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        is_inference = scope['type'] == 'http' and scope['method'] == 'POST' and scope['path'] == '/v1/systemone'
        if not is_inference:
            return await self.app(scope, receive, send)
        if self.state.serving or self.state.lock.locked():
            body = b'{"error":"busy","detail":"The previous GPU request is still running."}'
            await send({'type': 'http.response.start', 'status': 409, 'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': body})
            return
        self.state.serving = True
        try:
            return await self.app(scope, receive, send)
        finally:
            self.state.serving = False


def main():
    root = Path.cwd()
    spec = importlib.util.spec_from_file_location('imajev_pinned_server', root / 'scripts/playground/server.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    original_create_app = module.create_app
    manifest = json.loads((root.parent / 'runtime-manifest.json').read_text())
    runtime_id = str(uuid.uuid4())
    def create_app(*args, **kwargs):
        app = original_create_app(*args, **kwargs)
        app.state.serving = False
        app.add_middleware(BusyGuard, state=app.state)
        @app.get('/v1/status')
        def status():
            return {'busy': app.state.serving or app.state.lock.locked(), 'runtime_id': runtime_id, 'provenance': manifest}
        # Upstream mounts a catch-all playground, so move our route ahead of it.
        route = app.router.routes.pop()
        app.router.routes.insert(0, route)
        return app
    module.create_app = create_app
    return module.main()


if __name__ == '__main__':
    raise SystemExit(main())
