"""Independent Linux GUI monitor; records stalls and exits without killing models."""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import time

from app.storage.atomic import atomic_json


def process_identity(pid):
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] in ('Z', 'X') else fields[19]
    except (OSError, IndexError):
        return None


def monitor(pid, identity, state_file, log_file, stall_seconds=30):
    logger = logging.getLogger('watchdog')
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.info('Watching GUI pid=%s start_identity=%s state=%s', pid, identity, state_file)
    latest = {}
    stalled = False
    try:
        while True:
            try:
                latest = json.loads(state_file.read_text())
            except (OSError, ValueError):
                pass
            if latest.get('finished'):
                logger.info('Application finished pid=%s reason=%s error=%s cleanup_confirmed=%s',
                            pid, latest.get('exit_reason'), latest.get('exception'), latest.get('cleanup_confirmed'))
                return
            if process_identity(pid) != identity:
                live = {role: info for role, info in latest.get('managed_processes', {}).items()
                        if process_identity(info['pid']) is not None}
                report = {'detected_at': datetime.now(timezone.utc).isoformat(), 'pid': pid,
                          'reason': 'GUI disappeared without a completed lifecycle record',
                          'last_state': latest, 'surviving_managed_processes': live}
                atomic_json(state_file.with_suffix('.exit.json'), report)
                logger.error('GUI disappeared without recorded shutdown pid=%s session=%s phase=%s request=%s surviving_models=%s',
                             pid, latest.get('session_id'), latest.get('phase'), latest.get('request'), live)
                logger.error('Inspect crash trace %s and OS logs; SIGKILL/OOM cannot be identified from Python alone.', latest.get('crash_file'))
                return
            age = time.monotonic() - latest.get('heartbeat_monotonic', time.monotonic())
            if age >= stall_seconds and not stalled:
                stalled = True
                logger.error('GUI heartbeat stalled seconds=%.1f pid=%s session=%s phase=%s busy=%s request=%s',
                             age, pid, latest.get('session_id'), latest.get('phase'), latest.get('busy'), latest.get('request'))
                dump_signal = latest.get('stack_dump_signal')
                if dump_signal:
                    try:
                        os.kill(pid, dump_signal)
                        logger.error('Requested all-thread stack dump signal=%s file=%s', dump_signal, latest.get('crash_file'))
                    except ProcessLookupError:
                        pass
            elif age < stall_seconds and stalled:
                logger.info('GUI heartbeat recovered pid=%s', pid)
                stalled = False
            time.sleep(.2)
    finally:
        handler.close()
        logger.removeHandler(handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--log-file', type=Path, required=True)
    parser.add_argument('--stall-seconds', type=float, default=30)
    args = parser.parse_args()
    monitor(args.pid, args.identity, args.state_file, args.log_file, args.stall_seconds)


if __name__ == '__main__':
    main()
