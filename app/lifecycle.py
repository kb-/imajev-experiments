"""Persistent GUI lifecycle, Python/Qt failures, fatal traces and hang evidence."""
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from PyQt6.QtCore import QTimer, QtMsgType, qInstallMessageHandler
from app.lifecycle_watchdog import process_identity
from app.storage.atomic import atomic_json


class LifecycleDiagnostics:
    def __init__(self, log_path, stall_seconds=30):
        self.pid = os.getpid()
        directory = Path(log_path).resolve().parent
        directory.mkdir(parents=True, exist_ok=True)
        self.state_file = directory / f'app-run-{self.pid}.json'
        self.crash_file = directory / f'app-crash-{self.pid}.log'
        self.watchdog_log = directory / f'app-watchdog-{self.pid}.log'
        self.logger = logging.getLogger('app.lifecycle')
        self.previous_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.handler = RotatingFileHandler(directory / 'app-lifecycle.log', maxBytes=2_000_000,
                                           backupCount=3, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        self.handler.setLevel(logging.INFO)
        self.app_logger = logging.getLogger('app')
        self.app_logger.addHandler(self.handler)
        self.trace = self.crash_file.open('a', encoding='utf-8')
        self.trace.write(f'Application pid={self.pid} started={datetime.now(timezone.utc).isoformat()}\n')
        self.trace.flush()
        self.previous_fault_enabled = faulthandler.is_enabled()
        faulthandler.enable(file=self.trace, all_threads=True)
        self.dump_signal = None
        if hasattr(signal, 'SIGUSR1') and signal.getsignal(signal.SIGUSR1) == signal.SIG_DFL:
            faulthandler.register(signal.SIGUSR1, file=self.trace, all_threads=True)
            self.dump_signal = int(signal.SIGUSR1)
        self.previous_exception = sys.excepthook
        self.previous_thread_exception = threading.excepthook
        self.previous_unraisable = sys.unraisablehook
        sys.excepthook = self.exception_hook
        threading.excepthook = self.thread_exception_hook
        sys.unraisablehook = self.unraisable_hook
        self.previous_qt_message = qInstallMessageHandler(self.qt_message)
        self.previous_signals = {}
        self.pending_signals = []
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
            signum = getattr(signal, name, None)
            if signum is not None:
                self.previous_signals[signum] = signal.getsignal(signum)
                signal.signal(signum, self.signal_received)
        self.controller = self.manager = self.window = self.timer = None
        self.application = None
        self.closed = False
        self.exception = None
        self.exception_handled = False
        self.exit_reason = 'normal'
        self.request_id = None
        self.request_started = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.last_written = self.last_logged = 0.
        self.previous_status = None
        self.watchdog = None
        self.pulse()
        identity = process_identity(self.pid)
        if identity is not None:
            command = [sys.executable, '-m', 'app.lifecycle_watchdog', '--pid', str(self.pid), '--identity', identity,
                       '--state-file', str(self.state_file), '--log-file', str(self.watchdog_log),
                       '--stall-seconds', str(stall_seconds)]
            try:
                self.watchdog = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                    stdin=subprocess.DEVNULL, stdout=self.trace, stderr=self.trace, start_new_session=True)
            except OSError:
                self.logger.exception('Could not start independent GUI watchdog')
        self.logger.info('Lifecycle diagnostics installed pid=%s parent_pid=%s crash=%s state=%s watchdog=%s',
                         self.pid, os.getppid(), self.crash_file, self.state_file, getattr(self.watchdog, 'pid', None))

    def signal_received(self, signum, _frame):
        # Keep an emergency record even if the Qt event loop cannot dispatch the
        # graceful close. File-descriptor output does not depend on log locks.
        os.write(self.trace.fileno(), f'Termination signal received: {signal.Signals(signum).name}\n'.encode())
        self.pending_signals.append(signum)

    def record_exception(self, exc_type, value, traceback, origin='main', handled=False):
        self.exception = f'{getattr(exc_type, "__name__", "unknown")}: {value}'
        self.exception_handled = handled
        if handled:
            self.exit_reason = origin + ' error'
        self.logger.critical('%s exception origin=%s pid=%s session=%s phase=%s',
                             'Handled' if handled else 'Unhandled', origin, self.pid, getattr(self.controller, 'session_id', None),
                             getattr(self.controller, 'phase', None), exc_info=(exc_type, value, traceback))
        if not self.closed:
            try:
                atomic_json(self.state_file, self.snapshot())
            except Exception:
                self.logger.exception('Could not snapshot exception context')

    def exception_hook(self, exc_type, value, traceback):
        self.record_exception(exc_type, value, traceback, 'Python/Qt callback')
        self.previous_exception(exc_type, value, traceback)

    def thread_exception_hook(self, args):
        if args.exc_type is not SystemExit:
            self.record_exception(args.exc_type, args.exc_value, args.exc_traceback,
                                  f'thread {getattr(args.thread, "name", None)}')
        self.previous_thread_exception(args)

    def unraisable_hook(self, args):
        self.record_exception(args.exc_type, args.exc_value, args.exc_traceback, 'unraisable')
        self.previous_unraisable(args)

    def qt_message(self, kind, context, message):
        severity = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
                    QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
                    QtMsgType.QtFatalMsg: logging.CRITICAL}.get(kind, logging.WARNING)
        self.logger.log(severity, 'Qt message type=%s source=%s:%s function=%s: %s',
                        kind.name, context.file, context.line, context.function, message)
        if self.previous_qt_message:
            self.previous_qt_message(kind, context, message)

    def attach(self, application, controller=None, manager=None, window=None):
        # Keep Qt alive until diagnostics.close(), even after the event-loop
        # helper returns and drops its own QApplication reference.
        self.application = application
        self.controller, self.manager, self.window = controller, manager, window
        if self.timer is None:
            self.timer = QTimer(application)
            self.timer.timeout.connect(self.pulse)
            self.timer.start(500)
            application.aboutToQuit.connect(self.about_to_quit)
        if controller:
            controller.changed.connect(self.pulse)
        self.pulse()

    def about_to_quit(self):
        self.logger.info('Qt aboutToQuit pid=%s', self.pid)
        self.pulse()

    def snapshot(self):
        now = time.monotonic()
        result = {'version': 1, 'pid': self.pid, 'started_at': self.started_at,
                  'updated_at': datetime.now(timezone.utc).isoformat(), 'heartbeat_monotonic': now,
                  'finished': False, 'exception': self.exception, 'crash_file': str(self.crash_file),
                  'stack_dump_signal': self.dump_signal, 'managed_processes': {}}
        try:
            for line in Path('/proc/self/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    result['gui_rss_kib'] = int(line.split()[1])
                    break
        except OSError:
            pass
        c = self.controller
        if c:
            ticket = c.inflight
            if ticket != self.request_id:
                self.request_id = ticket
                self.request_started = now if ticket else None
            result.update(session_id=c.session_id, game=c.game.id, revision=c.game.revision(c.state),
                          phase=c.phase, ready=c.ready, busy=c.busy, stopping=c.stopping,
                          shutdown_done=c.shutdown_done, pending_strokes=len(c.pending),
                          shutdown_error=getattr(c, 'shutdown_error', None),
                          request=({'id': ticket.request_id, 'purpose': ticket.purpose,
                                    'revision': ticket.state_revision} if ticket else None),
                          request_age_seconds=round(now-self.request_started, 1) if ticket and self.request_started else None,
                          session_file=str((Path(c.config.directory) / c.session_id / 'session.json').resolve()))
        if self.manager:
            for role, name in (('imajev', 'process'), ('local_model', 'opponent_process'), ('ollama', 'ollama_process')):
                process = getattr(self.manager, name, None)
                if process:
                    result['managed_processes'][role] = {'pid': process.pid, 'returncode': process.poll()}
        return result

    def pulse(self):
        if self.closed:
            return
        state = self.snapshot()
        now = state['heartbeat_monotonic']
        status = {key: state.get(key) for key in ('session_id', 'game', 'revision', 'phase', 'busy', 'ready',
                                                'stopping', 'shutdown_done', 'request', 'pending_strokes', 'managed_processes')}
        if status != self.previous_status or now - self.last_written >= 2:
            try:
                atomic_json(self.state_file, state)
                self.last_written = now
            except OSError:
                self.logger.exception('Cannot write GUI lifecycle snapshot %s', self.state_file)
        if status != self.previous_status or now - self.last_logged >= 30:
            self.logger.info('GUI heartbeat pid=%s session=%s revision=%s phase=%s busy=%s request=%s age=%s models=%s',
                             self.pid, state.get('session_id'), state.get('revision'), state.get('phase'),
                             state.get('busy'), state.get('request'), state.get('request_age_seconds'), state['managed_processes'])
            self.last_logged = now
        self.previous_status = status
        if self.pending_signals and self.window:
            signals = self.pending_signals[:]
            self.pending_signals.clear()
            self.exit_reason = 'signal ' + signal.Signals(signals[-1]).name
            self.logger.warning('Termination requested signals=%s; starting normal window cleanup',
                                [signal.Signals(signum).name for signum in signals])
            self.window.close()

    def close(self):
        if self.closed:
            return
        if self.timer:
            self.timer.stop()
        state = self.snapshot()
        reason = 'unhandled exception' if self.exception and not self.exception_handled else self.exit_reason
        if getattr(self, 'cleanup_confirmed', None) is False:
            reason += '; service cleanup unconfirmed'
        state.update(finished=True, exit_reason=reason,
                     cleanup_confirmed=getattr(self, 'cleanup_confirmed', None))
        try:
            atomic_json(self.state_file, state)
        except OSError:
            self.logger.exception('Could not write completed lifecycle record %s', self.state_file)
        self.closed = True
        self.logger.info('Application lifecycle finished pid=%s reason=%s exception=%s',
                         self.pid, state['exit_reason'], self.exception)
        if self.watchdog:
            try:
                self.watchdog.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.logger.error('Lifecycle watchdog did not acknowledge completed shutdown pid=%s', self.watchdog.pid)
                # Stop our monitor before restoring SIGUSR1; it must never send
                # a dump signal after that signal's fatal default is restored.
                self.watchdog.terminate()
                try:
                    self.watchdog.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.watchdog.kill()
                    self.watchdog.wait(timeout=3)
        for signum, previous in self.previous_signals.items():
            signal.signal(signum, previous)
        if self.dump_signal:
            faulthandler.unregister(self.dump_signal)
        if self.previous_fault_enabled:
            faulthandler.enable(file=sys.stderr, all_threads=True)
        else:
            faulthandler.disable()
        sys.excepthook = self.previous_exception
        threading.excepthook = self.previous_thread_exception
        sys.unraisablehook = self.previous_unraisable
        qInstallMessageHandler(self.previous_qt_message)
        self.trace.close()
        self.app_logger.removeHandler(self.handler)
        self.handler.close()
        self.logger.setLevel(self.previous_level)
