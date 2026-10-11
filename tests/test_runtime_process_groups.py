"""An exited launcher must not leave its model worker behind during a swap."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app.config import Config
from app.inference.processes import process_group_alive, terminate_process_group
from app.inference.service_manager import ServiceManager

pytestmark = pytest.mark.skipif(not Path('/proc').is_dir() or not hasattr(os, 'killpg'),
                                reason='Linux runtime process groups')


def launcher_with_worker(tmp_path, exit_launcher):
    ready = tmp_path / 'worker.pid'
    worker = '''
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
'''
    launcher = '''
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])
if sys.argv[3] == 'stay': time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, '-c', launcher, worker, str(ready),
                                'exit' if exit_launcher else 'stay'], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError('Worker did not report readiness')
            time.sleep(.01)
        worker_pid = int(ready.read_text())
        if exit_launcher:
            process.wait(timeout=5)
        return process, worker_pid
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise


@pytest.mark.parametrize('role', ['imajev', 'opponent', 'relay'])
@pytest.mark.parametrize('exit_launcher', [False, True])
def test_manager_confirms_worker_exit_even_after_launcher_exits(tmp_path, monkeypatch, caplog, role, exit_launcher):
    process, worker_pid = launcher_with_worker(tmp_path, exit_launcher)
    manager = ServiceManager(Config())
    monkeypatch.setattr('app.inference.service_manager.terminate_process_group',
                        lambda child, label, **_: terminate_process_group(child, label, timeout=.1, kill_timeout=2))
    caplog.set_level('INFO', logger='app.inference.processes')
    try:
        assert process_group_alive(process.pid)
        if role == 'imajev':
            manager.process = process
            manager.stop()
            assert manager.process is None
        elif role == 'opponent':
            manager.opponent_process = process
            manager._release_opponent()
            assert manager.opponent_process is None
        else:
            manager.ollama_process = process
            manager.shutdown()
            assert manager.ollama_process is None
        assert process.poll() is not None
        assert not process_group_alive(process.pid)
        stat = Path(f'/proc/{worker_pid}/stat')
        if stat.exists():
            assert stat.read_text().rsplit(')', 1)[1].split()[0] in ('Z', 'X')
        assert 'termination timed out; killing process group' in caplog.text
        assert f'process group exit confirmed pgid={process.pid}' in caplog.text
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def test_unconfirmed_worker_exit_blocks_ollama_coaching(monkeypatch):
    manager = ServiceManager(Config(coach_backend='ollama', coach_model='coach'))
    process = SimpleNamespace(pid=12345, poll=lambda: 0)
    manager.opponent_process = process
    def blocked(*_, **__):
        raise subprocess.TimeoutExpired('process group 12345', 10)
    monkeypatch.setattr('app.inference.service_manager.terminate_process_group', blocked)
    monkeypatch.setattr('app.inference.service_manager.httpx.Client',
                        lambda **_: pytest.fail('Ollama contacted before worker exit confirmation'))
    with pytest.raises(subprocess.TimeoutExpired):
        manager.ollama(lambda _: pytest.fail('Coach started before worker exit confirmation'))
    assert manager.opponent_process is process
