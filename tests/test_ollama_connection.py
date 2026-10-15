import io
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.config import Config
from app.inference import windows_ollama_bridge as bridge
from app.inference.service_manager import ServiceManager


def test_relay_responses_survive_deleted_terminal(monkeypatch, tmp_path):
    class DeletedTerminal:
        def write(self, _):
            raise OSError(5, 'Input/output error')

        def flush(self):
            pass

    calls = []

    def forward(method, path, body, port):
        calls.append(path)
        if path == '/api/chat':
            raise RuntimeError('Windows Ollama is unavailable')
        return 200, b'{"models":[]}'

    monkeypatch.setattr(bridge, 'forward', forward)
    handler = bridge.configure_relay_logging(tmp_path / 'relay.log')
    server = bridge.RelayServer(('127.0.0.1', 0), bridge.Handler)
    server.windows_port = 11434
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(sys, 'stderr', DeletedTerminal())
            with httpx.Client(trust_env=False, timeout=3) as client:
                url = f'http://127.0.0.1:{server.server_port}'
                response = client.get(url + '/api/ps')
                assert response.status_code == 200
                assert response.json() == {'models': []}
                response = client.post(url + '/api/chat', json={})
                assert response.status_code == 502
                assert 'Windows Ollama is unavailable' in response.json()['error']
        assert calls == ['/api/ps', '/api/chat']
        log = (tmp_path / 'relay.log').read_text()
        assert 'GET /api/ps' in log and 'POST /api/chat' in log
        assert 'Traceback' in log and 'Windows Ollama is unavailable' in log
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        bridge.logger.removeHandler(handler)
        handler.close()


def test_foreign_disconnected_endpoint_is_not_replaced(monkeypatch):
    manager = ServiceManager(Config(coach_backend='ollama', coach_model='coach'))
    client = SimpleNamespace(get=lambda *a, **kw: (_ for _ in ()).throw(
        httpx.RemoteProtocolError('Server disconnected without sending a response.')))
    monkeypatch.setattr('app.inference.service_manager.subprocess.Popen',
                        lambda *a, **kw: pytest.fail('Replaced an unowned endpoint'))
    with pytest.raises(RuntimeError, match=r'127.0.0.1:11434/api/ps.*RemoteProtocolError'):
        manager._ensure_ollama(client)


def test_windows_error_body_survives_connection_check(monkeypatch):
    manager = ServiceManager(Config(coach_backend='ollama', coach_model='coach'))
    def get(url, **kwargs):
        response = httpx.Response(502, json={'error': 'Windows Ollama request failed: connection refused'},
                                  request=httpx.Request('GET', url))
        return response
    monkeypatch.setattr('app.inference.service_manager.subprocess.Popen',
                        lambda *a, **kw: pytest.fail('Replaced a live endpoint'))
    with pytest.raises(RuntimeError, match='Windows Ollama request failed: connection refused'):
        manager._ensure_ollama(SimpleNamespace(get=get))


def test_owned_broken_relay_is_stopped_before_replacement(monkeypatch):
    from app.inference import service_manager as sm
    manager = ServiceManager(Config(coach_backend='ollama', coach_model='coach'))
    old = SimpleNamespace(pid=1234, poll=lambda: None)
    new = SimpleNamespace(pid=5678, poll=lambda: None)
    manager.ollama_process = old
    manager.ollama_is_relay = True
    old_log = io.StringIO()
    manager.ollama_log = old_log
    trace = []
    answers = iter([httpx.RemoteProtocolError('disconnected'), None])

    def get(*a, **kw):
        answer = next(answers)
        if answer:
            raise answer
        trace.append('ready')
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(sm, 'terminate_process_group', lambda p, *a, **kw: trace.append(('exit', p.pid)))
    monkeypatch.setattr(sm.shutil, 'which', lambda _: None)
    original_exists = sm.Path.exists
    monkeypatch.setattr(sm.Path, 'exists', lambda p: True if str(p).endswith('powershell.exe') else original_exists(p))

    def spawn(command, **kwargs):
        trace.append('spawn')
        assert old_log.closed
        assert kwargs['stdin'] == sm.subprocess.DEVNULL
        assert kwargs['stderr'] == sm.subprocess.STDOUT
        assert kwargs['stdout'] is manager.ollama_log
        assert 'windows_ollama_bridge' in command[2]
        return new

    monkeypatch.setattr(sm.subprocess, 'Popen', spawn)
    try:
        manager._ensure_ollama(SimpleNamespace(get=get))
        assert trace == [('exit', 1234), 'spawn', 'ready']
        assert manager.ollama_process is new
    finally:
        if manager.ollama_log:
            manager.ollama_log.close()


def test_failed_ollama_start_cleans_up_owned_process(monkeypatch):
    from app.inference import service_manager as sm
    manager = ServiceManager(Config(coach_backend='ollama', coach_model='coach'))
    process = SimpleNamespace(pid=2345, poll=lambda: 1)
    trace = []
    monkeypatch.setattr(sm.shutil, 'which', lambda _: '/usr/bin/ollama')
    monkeypatch.setattr(sm.subprocess, 'Popen', lambda *a, **kw: process)
    monkeypatch.setattr(sm, 'terminate_process_group', lambda p, *a, **kw: trace.append(p.pid))
    client = SimpleNamespace(get=lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError('unavailable')))
    with pytest.raises(RuntimeError, match='did not become ready.*ConnectError'):
        manager._ensure_ollama(client)
    assert trace == [2345]
    assert manager.ollama_process is None and manager.ollama_log is None


def test_imajev_preflight_reports_ollama_failure_before_launch(monkeypatch):
    from app.inference import service_manager as sm
    manager = ServiceManager(Config(coach_backend='ollama', coach_model='coach'))
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def connect_ex(self, address): return 1

    monkeypatch.setattr(sm.socket, 'socket', Probe)
    monkeypatch.setattr(manager, '_ensure_ollama', lambda c: (_ for _ in ()).throw(RuntimeError('relay disconnected')))
    monkeypatch.setattr(sm.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Unsafe GPU allocation'))
    with pytest.raises(RuntimeError, match='ImaJEV recognition startup blocked.*GPU-memory.*relay disconnected'):
        manager.start()
