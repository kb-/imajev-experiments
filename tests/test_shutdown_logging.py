"""Closing a window leaves enough evidence to diagnose background cleanup."""
import logging
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app.logging_setup import configure_logging
from app.config import Config
from app.inference.service_manager import ServiceManager
from app.ui.window import Window
from test_protocol import INK
from test_session import make, wait_for


def test_normal_launch_writes_lifecycle_log_without_pointer_debug(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = logging.getLogger('app')
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    # Preserve pytest/caller handlers; configure_logging closes attached handlers.
    logger.handlers = []
    try:
        path = configure_logging()
        logging.getLogger('app.main').info('Application exit cleanup complete pid=123')
        logging.getLogger('app.ui.canvas').debug('mouse_move')
        assert path == tmp_path / 'logs/app.log'
        text = path.read_text()
        assert 'Application exit cleanup complete pid=123' in text
        assert 'detailed mouse logging=False' in text
        assert 'mouse_move' not in text
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_close_logs_pending_request_then_owned_cleanup(qapp, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='app')
    c, client = make(qapp)
    monkeypatch.setattr(c, 'start', lambda: None)
    client.gate.clear()
    c.set_pending(INK)
    c.submit()
    wait_for(qapp, lambda: len(client.calls) == 1)
    stopped = []
    c.service_manager = SimpleNamespace(shutdown=lambda: stopped.append(True))
    view = Window(c)
    try:
        view.close()
        assert c.stopping and c.busy and not c.shutdown_done
        assert not stopped
        assert 'Window close requested' in caplog.text
        assert "purpose='recognition'" in caplog.text
        view.close_last_warning = time.monotonic() - 31
        view.finish_close()
        assert 'Window hidden; shutdown still pending' in caplog.text
        client.gate.set()
        wait_for(qapp, lambda: c.shutdown_done)
        view.finish_close()
        assert stopped == [True]
        assert 'Inflight request finished during shutdown purpose=recognition' in caplog.text
        assert 'Service cleanup starting' in caplog.text
        assert 'Service cleanup finished' in caplog.text
        assert 'Window close accepted; shutdown complete' in caplog.text
        assert c.state.revision == 0
    finally:
        client.gate.set()
        wait_for(qapp, lambda: not c.busy)
        view.close()


def test_cleanup_error_is_logged(qapp, monkeypatch, caplog):
    c, _ = make(qapp)
    monkeypatch.setattr(c, 'start', lambda: None)

    def fail():
        raise RuntimeError('unload is unconfirmed')

    c.service_manager = SimpleNamespace(shutdown=fail)
    view = Window(c)
    view.close()
    wait_for(qapp, lambda: c.shutdown_done)
    assert 'Service shutdown: unload is unconfirmed' in caplog.text
    view.close()


@pytest.mark.skipif(not hasattr(os, 'killpg'), reason='Managed runtimes require POSIX process groups')
def test_owned_child_exit_is_confirmed_and_logged(caplog):
    caplog.set_level(logging.INFO, logger='app')
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             start_new_session=True)
    manager = ServiceManager(Config())
    manager.opponent_process = child
    try:
        manager.shutdown()
        assert child.poll() is not None
        assert manager.opponent_process is None
        assert 'Shutdown waiting for GPU ownership lock' in caplog.text
        assert 'Shutdown acquired GPU ownership lock' in caplog.text
        assert f'Stopping opponent process group pid={child.pid}' in caplog.text
        assert f'Opponent process exited pid={child.pid}' in caplog.text
        assert 'Runtime cleanup complete' in caplog.text
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize('managed', [True, False])
def test_hidden_window_cleanup_exits_application_event_loop(managed):
    script = '''
import sys
from types import SimpleNamespace
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.ui.window import Window
app = QApplication([])
c = SessionController(TicTacToe(), object(), Config())
c.start = lambda: None
c.service_manager = SimpleNamespace(shutdown=lambda: None) if sys.argv[1] == 'managed' else None
w = Window(c)
w.closed.connect(app.quit)
w.show()
QTimer.singleShot(0, w.close)
QTimer.singleShot(2000, lambda: app.exit(9))
result = app.exec()
assert c.shutdown_done
sys.exit(result)
'''
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([sys.executable, '-c', script, 'managed' if managed else 'unmanaged'], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
