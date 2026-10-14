"""Shared file identities, atomic manifests, and managed inference validation."""
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(path):
    path = Path(path).resolve()
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest(path)}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def output(command):
    return subprocess.check_output(list(map(str, command)), text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def active_inference(root):
    """Find this checkout's running GUI or model runtimes, never terminate them."""
    active = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            cwd = (entry / 'cwd').resolve(strict=True)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        in_checkout = cwd == root or root in cwd.parents or any(str(root) in arg for arg in args)
        is_runtime = any(Path(arg).name in ('llama-server', 'serve_local.py', 'windows_ollama_bridge.py') for arg in args)
        is_app = any(args[i:i+2] == ['-m', 'app.main'] for i in range(len(args)))
        is_cli = any(Path(arg).name == 'imajev-game' for arg in args)
        if in_checkout and (is_runtime or is_app or is_cli):
            active.append(int(entry.name))
    return active


def gpu_snapshot():
    executable = shutil.which('nvidia-smi') or '/usr/lib/wsl/lib/nvidia-smi'
    raw = output([executable, '--query-gpu=index,name,memory.used,memory.total', '--format=csv,noheader,nounits'])
    return [{'index': int(row[0]), 'name': row[1].strip(), 'used_mib': float(row[2]), 'total_mib': float(row[3])}
            for row in csv.reader(raw.splitlines())]


class GPUSampler:
    """Sample observed whole-device VRAM; do not confuse this with allocation peak."""
    def __init__(self):
        self.samples = []
        self.errors = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.phase = 'startup'

    def sample(self):
        while not self.done.is_set():
            try:
                self.samples.append({'elapsed_seconds': time.monotonic() - self.started,
                                     'phase': self.phase, 'gpus': gpu_snapshot()})
            except Exception as exc:
                self.errors.append(str(exc))
                return
            self.done.wait(.2)

    def start(self):
        self.started = time.monotonic()
        self.samples.append({'elapsed_seconds': 0, 'phase': self.phase, 'gpus': gpu_snapshot()})
        self.thread.start()

    def stop(self):
        self.done.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError('GPU sampler did not stop.')
        if self.errors:
            raise RuntimeError('GPU sampling failed: ' + '; '.join(self.errors))

