"""Confirm termination of owned POSIX runtime groups, including worker children."""
import logging
import os
from pathlib import Path
import signal
import subprocess
import time

logger = logging.getLogger(__name__)


def process_group_alive(pgid):
    proc = Path('/proc')
    if proc.is_dir():
        # killpg(pgid, 0) also finds zombies, which no longer hold model memory.
        # A launcher can exit before its Python/CUDA worker, so inspect the group.
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text(errors='replace').rsplit(')', 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == pgid and fields[0] not in ('Z', 'X'):
                return True
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def terminate_process_group(process, label, timeout=30, kill_timeout=10):
    """Return only after the launcher and all live members have exited."""
    def send(sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def wait(seconds):
        deadline = time.monotonic() + seconds
        process.wait(timeout=seconds)
        while process_group_alive(process.pid):
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(f'process group {process.pid}', seconds)
            time.sleep(.05)

    if process.poll() is None or process_group_alive(process.pid):
        logger.info('Stopping %s process group pid=%s', label, process.pid)
        send(signal.SIGTERM)
        try:
            wait(timeout)
        except subprocess.TimeoutExpired:
            logger.warning('%s termination timed out; killing process group pid=%s', label, process.pid)
            send(signal.SIGKILL)
            wait(kill_timeout)
    logger.info('%s process exited pid=%s returncode=%s', label[0].upper() + label[1:], process.pid, process.poll())
    logger.info('%s process group exit confirmed pgid=%s', label, process.pid)
