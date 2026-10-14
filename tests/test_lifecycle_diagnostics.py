"""Exercise failure evidence in disposable processes; never crash the test runner."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONPATH=str(ROOT))


def wait_for_file(path, predicate=lambda _: True, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and predicate(path.read_text()):
            return path.read_text()
        time.sleep(.05)
    raise AssertionError(f'Diagnostic did not arrive: {path}')


def run(script, directory):
    return subprocess.Popen([sys.executable, '-c', script, str(directory)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.skipif(not Path('/proc').is_dir(), reason='Independent Linux watchdog')
def test_abrupt_gui_kill_is_logged_by_independent_watchdog(tmp_path):
    script = '''
import sys,time
from pathlib import Path
from app.lifecycle import LifecycleDiagnostics
d = LifecycleDiagnostics(Path(sys.argv[1])/'app.log')
time.sleep(60)
'''
    child = run(script, tmp_path)
    try:
        state = tmp_path / f'app-run-{child.pid}.json'
        wait_for_file(state)
        child.kill()
        child.communicate(timeout=5)
        report = json.loads(wait_for_file(state.with_suffix('.exit.json')))
        assert report['reason'] == 'GUI disappeared without a completed lifecycle record'
        assert report['last_state']['pid'] == child.pid
        assert not report['last_state']['finished']
        assert 'without recorded shutdown' in wait_for_file(tmp_path / f'app-watchdog-{child.pid}.log',
                                                            lambda text: 'without recorded shutdown' in text)
    finally:
        if child.poll() is None:
            child.kill(); child.communicate(timeout=5)


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1'), reason='Native thread-dump signal')
def test_blocked_qt_loop_produces_thread_dump_and_recovery_log(tmp_path):
    script = '''
import sys,time
from pathlib import Path
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from app.lifecycle import LifecycleDiagnostics
app = QApplication([])
d = LifecycleDiagnostics(Path(sys.argv[1])/'app.log', stall_seconds=.8)
d.attach(app)
QTimer.singleShot(100, lambda: time.sleep(2.0))
QTimer.singleShot(3000, app.quit)
app.exec()
d.close()
'''
    child = run(script, tmp_path)
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    trace = (tmp_path / f'app-crash-{child.pid}.log').read_text()
    assert 'lambda' in trace and 'Current thread' in trace
    watchdog = (tmp_path / f'app-watchdog-{child.pid}.log').read_text()
    assert 'GUI heartbeat stalled' in watchdog
    assert 'GUI heartbeat recovered' in watchdog
    assert 'Application finished' in watchdog


@pytest.mark.skipif(not hasattr(signal, 'SIGABRT'), reason='Native fatal-signal trace')
def test_native_abort_writes_all_thread_trace(tmp_path):
    script = '''
import os,resource,sys,time
from pathlib import Path
from app.lifecycle import LifecycleDiagnostics
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
d = LifecycleDiagnostics(Path(sys.argv[1])/'app.log')
os.abort()
'''
    child = run(script, tmp_path)
    child.communicate(timeout=10)
    assert child.returncode == -signal.SIGABRT
    trace = (tmp_path / f'app-crash-{child.pid}.log').read_text()
    assert 'Fatal Python error: Aborted' in trace
    wait_for_file(tmp_path / f'app-run-{child.pid}.exit.json')


def test_python_thread_qt_and_worker_failures_have_tracebacks(tmp_path):
    script = '''
import sys,threading
from pathlib import Path
from types import SimpleNamespace
from PyQt6.QtCore import qWarning
from app.lifecycle import LifecycleDiagnostics
from app.inference.jobs import Job
d = LifecycleDiagnostics(Path(sys.argv[1])/'app.log')
def fail(): raise RuntimeError('diagnostic thread failure')
t = threading.Thread(target=fail,name='example-worker'); t.start(); t.join()
qWarning('diagnostic Qt warning')
Job(SimpleNamespace(purpose='recognition',request_id='example-request'), fail).run()
try: raise ValueError('diagnostic callback failure')
except ValueError:
    d.exception_hook(*sys.exc_info())
d.close()
'''
    child = run(script, tmp_path)
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    log = (tmp_path / 'app-lifecycle.log').read_text()
    for text in ('example-worker', 'diagnostic Qt warning', 'Background operation failed',
                 'example-request', 'diagnostic callback failure', 'Traceback'):
        assert text in log
    state = json.loads((tmp_path / f'app-run-{child.pid}.json').read_text())
    assert state['finished'] and state['exit_reason'] == 'unhandled exception'


def test_termination_signal_is_logged_and_requests_window_close(tmp_path):
    script = '''
import os,signal,sys
from pathlib import Path
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication,QWidget
from app.lifecycle import LifecycleDiagnostics
app = QApplication([])
w = QWidget(); w.show()
d = LifecycleDiagnostics(Path(sys.argv[1])/'app.log')
d.attach(app,window=w)
QTimer.singleShot(100, lambda: os.kill(os.getpid(),signal.SIGTERM))
QTimer.singleShot(3000, lambda: app.exit(9))
code = app.exec()
d.close()
raise SystemExit(code)
'''
    child = run(script, tmp_path)
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    assert 'Termination signal received: SIGTERM' in (tmp_path / f'app-crash-{child.pid}.log').read_text()
    state = json.loads((tmp_path / f'app-run-{child.pid}.json').read_text())
    assert state['finished'] and state['exit_reason'] == 'signal SIGTERM'


def test_snapshot_identifies_session_request_and_owned_models(tmp_path):
    script = '''
import os,sys
from pathlib import Path
from types import SimpleNamespace
from app.config import Config
from app.games.tic_tac_toe.game import TicTacToe
from app.lifecycle import LifecycleDiagnostics
d = LifecycleDiagnostics(Path(sys.argv[1])/'app.log')
game = TicTacToe()
d.controller = SimpleNamespace(session_id='saved-session',game=game,state=game.initial_state(),
    inflight=SimpleNamespace(request_id='request-123',purpose='recognition',state_revision=0),
    phase='recognition',ready=True,busy=True,stopping=False,shutdown_done=False,pending=(object(),),config=Config())
d.manager = SimpleNamespace(opponent_process=SimpleNamespace(pid=12345,poll=lambda:None))
d.pulse()
d.close()
'''
    child = run(script, tmp_path)
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    state = json.loads((tmp_path / f'app-run-{child.pid}.json').read_text())
    assert state['session_id'] == 'saved-session' and state['revision'] == 0
    assert state['busy'] and state['request']['id'] == 'request-123'
    assert state['request']['purpose'] == 'recognition'
    assert state['pending_strokes'] == 1
    assert state['managed_processes']['local_model']['pid'] == 12345
    assert state['session_file'].endswith('/sessions/saved-session/session.json')


def test_diagnostics_keeps_qt_alive_until_cleanup_after_event_loop_returns(tmp_path):
    script = '''
import sys
from pathlib import Path
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from app.lifecycle import LifecycleDiagnostics
d = LifecycleDiagnostics(Path(sys.argv[1])/'app.log')
def loop():
    app = QApplication([])
    d.attach(app)
    QTimer.singleShot(0, app.quit)
    return app.exec()
assert loop() == 0
d.close()
'''
    child = run(script, tmp_path)
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    state = json.loads((tmp_path / f'app-run-{child.pid}.json').read_text())
    assert state['finished'] and state['exit_reason'] == 'normal'
