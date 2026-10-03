import time
from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


class JobSignals(QObject):
    completed = pyqtSignal(object, object, object, float)


class Job(QRunnable):
    def __init__(self, ticket, operation):
        super().__init__()
        self.ticket = ticket
        self.operation = operation
        self.signals = JobSignals()

    def run(self):
        start = time.monotonic()
        reply = error = None
        try:
            reply = self.operation()
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        self.signals.completed.emit(self.ticket, reply, error, time.monotonic() - start)
