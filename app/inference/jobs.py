import time
import logging
from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

logger = logging.getLogger(__name__)


class JobSignals(QObject):
    completed = pyqtSignal(object, object, object, float)


class Job(QRunnable):
    def __init__(self, ticket, operation):
        super().__init__()
        self.ticket = ticket
        self.operation = operation
        self.error_raw = None
        self.signals = JobSignals()

    def run(self):
        start = time.monotonic()
        reply = error = None
        try:
            reply = self.operation()
        except Exception as exc:
            logger.exception('Background operation failed ticket=%s', self.ticket)
            error = str(exc) or type(exc).__name__
            self.error_raw = getattr(exc, 'raw', None)
        self.signals.completed.emit(self.ticket, reply, error, time.monotonic() - start)
