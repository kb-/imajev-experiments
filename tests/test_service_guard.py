import asyncio
import threading
from types import SimpleNamespace
from scripts.serve_local import BusyGuard


def test_busy_gate_rejects_overlapping_gpu_requests():
    async def scenario():
        state = SimpleNamespace(serving=False, lock=threading.Lock())
        gate = asyncio.Event()
        calls, responses = [], []
        async def endpoint(scope, receive, send):
            calls.append(scope['path'])
            await gate.wait()
        async def receive(): return {'type': 'http.request', 'body': b''}
        async def send(message): responses.append(message)
        app = BusyGuard(endpoint, state)
        scope = {'type': 'http', 'method': 'POST', 'path': '/v1/systemone'}
        first = asyncio.create_task(app(scope, receive, send))
        await asyncio.sleep(0)
        assert state.serving
        await app(scope, receive, send)
        assert len(calls) == 1 and responses[0]['status'] == 409
        gate.set()
        await first
        assert not state.serving
        state.lock.acquire()  # GPU may outlive an aborted HTTP handler.
        await app(scope, receive, send)
        assert len(calls) == 1 and responses[-2]['status'] == 409
        state.lock.release()
    asyncio.run(scenario())
