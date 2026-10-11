"""Install pinned Decision 2.0 Nox Q8 weights and its separate CUDA runtime."""
import argparse
import csv
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'https://github.com/Xunzhuo/llama.cpp.git'
COMMIT = '3c64a581d4f95bb2de409d383741fa5b18d84345'
MODEL_REPOSITORY = 'vllm-sr/Decision-2.0-Nox-4B-GGUF'
REVISION = 'b179d65af24ff9e28924c8d5c080f8c5bf5b2b07'
FILENAME = 'Decision-2.0-Nox-4B-Q8_0.gguf'
SHA256 = '047979ef73ffd92f2619cc4f4ede1e5996b42dd808d97b5cdc01aa75e0130390'
SIZE = 4492932832
URL = f'https://huggingface.co/{MODEL_REPOSITORY}/resolve/{REVISION}/{FILENAME}'
PATCH_FILE = 'tools/server/server-common.cpp'
PATCH_ORIGINAL_SHA256 = '8ea2b7de77da8e67b5624e4583e27ffae200fd03a30969d64951feff31e6dab6'
PATCH_SHA256 = '4bb9f7d91834eb7e79f89858e2f2b8946e8c2f55dc3da9160dd343ba368b02d3'
PATCHES = [{'name': 'decision2-output-modalities-v1', 'file': PATCH_FILE,
            'original_sha256': PATCH_ORIGINAL_SHA256, 'sha256': PATCH_SHA256}]


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


def run(command, **kwargs):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def output(command):
    return subprocess.check_output(list(map(str, command)), text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def prerequisites():
    choices = {'Git': ('git',), 'CMake': ('cmake',), 'C++ compiler': ('c++', 'g++', 'clang++'),
               'CUDA nvcc': ('nvcc',), 'build tool': ('make', 'ninja')}
    found = {label: next((shutil.which(name) for name in names if shutil.which(name)), None)
             for label, names in choices.items()}
    missing = [name for name, path in found.items() if path is None]
    if missing:
        raise RuntimeError('Missing prerequisites: ' + ', '.join(missing) + '.\n'
                           'On Ubuntu/WSL install git, cmake, build-essential and libcurl4-openssl-dev. '
                           'Install the NVIDIA CUDA toolkit for WSL (nvcc) and add its bin directory to PATH. '
                           'Keep the existing Windows NVIDIA driver; this script installs no system packages. '
                           'Use --download-only if you only want the Q8 weights.')
    return found


def download(directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / FILENAME
    if destination.exists():
        if destination.stat().st_size != SIZE or digest(destination) != SHA256:
            raise ValueError(f'Existing model differs from pinned Q8 weights: {destination}. Move it aside first.')
        print(f'Q8 weights already verified: {destination}', flush=True)
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=FILENAME + '.', suffix='.part', delete=False) as stream:
            temporary = Path(stream.name)
            checksum = hashlib.sha256()
            received = 0
            progress = 0
            print(f'Downloading {FILENAME} ({SIZE / 1e9:.2f} GB), revision {REVISION}', flush=True)
            with httpx.stream('GET', URL, follow_redirects=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 1024):
                    stream.write(chunk)
                    checksum.update(chunk)
                    received += len(chunk)
                    if received - progress >= 256 * 1024 * 1024:
                        print(f'  {received / 1e9:.2f} / {SIZE / 1e9:.2f} GB', flush=True)
                        progress = received
            stream.flush()
            os.fsync(stream.fileno())
        if received != SIZE or checksum.hexdigest() != SHA256:
            raise ValueError('Downloaded Q8 model size or SHA-256 differs from the pinned publisher asset.')
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_checkout(source):
    if not (source / '.git').is_dir():
        raise ValueError(f'Runtime directory is not an installer checkout: {source}. Move it aside first.')
    if output(['git', '-C', source, 'rev-parse', 'HEAD']) != COMMIT:
        raise ValueError('Runtime checkout is not at the pinned Decision 2.0 commit. Move it aside first.')
    status = output(['git', '-C', source, 'status', '--porcelain', '--untracked-files=all']).strip()
    known_patch = status == 'M ' + PATCH_FILE and digest(source / PATCH_FILE) == PATCH_SHA256
    if status and not known_patch:
        raise ValueError('Runtime checkout has modified or untracked source files. Preserve them before installing.')


def patch_runtime(source):
    path = source / PATCH_FILE
    checksum = digest(path)
    if checksum == PATCH_SHA256:
        return
    if checksum != PATCH_ORIGINAL_SHA256:
        raise ValueError('Runtime metadata source differs from the pinned compatibility patch input.')
    original = path.read_text()
    anchor = '        case COMMON_DECISION_TYPE_OPENJEV:\n'
    patched = original.replace(anchor, anchor + '        case COMMON_DECISION_TYPE_DECISION2:\n', 1)
    if hashlib.sha256(patched.encode()).hexdigest() != PATCH_SHA256:
        raise ValueError('Runtime compatibility patch checksum differs.')
    path.write_text(patched)
    print('Applied pinned Decision 2.0 output-modality metadata fix.', flush=True)


def validate_runtime(source, runtime, require_patch=True):
    if not source.exists():
        raise RuntimeError('Decision 2.0 CUDA runtime is not installed. Run the complete installer after installing nvcc.')
    check_checkout(source)
    if not runtime or runtime.get('commit') != COMMIT:
        raise ValueError('Missing or incompatible runtime manifest; run the complete installer.')
    if runtime.get('build_options', {}).get('cuda') is not True:
        raise ValueError('Runtime manifest does not describe a CUDA build.')
    if require_patch and (runtime.get('patches') != PATCHES or digest(source / PATCH_FILE) != PATCH_SHA256):
        raise ValueError('Runtime needs the Decision 2.0 metadata fix. Rerun the installer; CUDA objects will be reused.')
    expected = source / 'build/bin/llama-server'
    executable = runtime.get('executable', {})
    if executable.get('path') != str(expected.resolve()):
        raise ValueError('Runtime manifest points to a different executable.')
    for entry in [executable, *runtime.get('libraries', [])]:
        if identity(entry['path']) != entry:
            raise ValueError(f'Installed runtime file differs from its manifest: {entry["path"]}')
    return expected


def build(root, tools, cuda_arch, jobs, previous):
    source = root / 'runtimes/decision2-src'
    if source.exists():
        check_checkout(source)
    else:
        source.parent.mkdir(parents=True, exist_ok=True)
        run(['git', 'init', source])
        run(['git', '-C', source, 'remote', 'add', 'origin', REPOSITORY])
        run(['git', '-C', source, 'fetch', '--depth', '1', 'origin', COMMIT])
        run(['git', '-C', source, 'checkout', '--detach', 'FETCH_HEAD'])
        check_checkout(source)
    options = {'cuda': True, 'cuda_arch': cuda_arch, 'build_type': 'Release'}
    executable = source / 'build/bin/llama-server'
    upgrade = False
    if previous and previous.get('build_options') == options:
        validate_runtime(source, previous, require_patch=False)
        if previous.get('patches') == PATCHES:
            validate_runtime(source, previous)
            print('Pinned CUDA runtime already verified.', flush=True)
            return previous
        if previous.get('patches'):
            raise ValueError('Runtime manifest contains an unknown patch set.')
        upgrade = True
    if not upgrade and (executable.exists() or (source / 'build').exists()):
        raise ValueError('Existing build has no matching manifest/options. Move build/ aside before rebuilding.')
    patch_runtime(source)
    command = [tools['CMake'], '-S', source, '-B', source / 'build', '-DCMAKE_BUILD_TYPE=Release',
               '-DGGML_CUDA=ON', f'-DCMAKE_CUDA_ARCHITECTURES={cuda_arch}',
               f'-DCMAKE_CUDA_COMPILER={tools["CUDA nvcc"]}', f'-DCMAKE_CXX_COMPILER={tools["C++ compiler"]}']
    if not shutil.which('make'):
        command += ['-G', 'Ninja']
    logs = root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / 'decision2-build.log').open('a') as log:
        run(command, stdout=log, stderr=subprocess.STDOUT)
        run([tools['CMake'], '--build', source / 'build', '--target', 'llama-server', '--parallel', jobs],
            stdout=log, stderr=subprocess.STDOUT)
    check_checkout(source)
    return {'repository': REPOSITORY, 'commit': COMMIT, 'build_options': options, 'patches': PATCHES,
            'cuda_compiler': output([tools['CUDA nvcc'], '--version']),
            'version': output([executable, '--version']), 'executable': identity(executable),
            'libraries': [identity(path) for path in sorted((source / 'build/bin').glob('*.so*'))
                          if path.is_file() and not path.is_symlink()]}


def install(root, download_only=False, cuda_arch='86', jobs=4):
    tools = None if download_only else prerequisites()
    manifest_path = root / 'runtimes/decision2/manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not download_only:
        # Record the successful build immediately, so a failed weight download is retryable.
        manifest['runtime'] = build(root, tools, cuda_arch, jobs, manifest.get('runtime'))
        write_json(manifest_path, manifest)
    model = download(root / 'models/decision2-nox-4b')
    manifest.update(version=1, model={'repository': MODEL_REPOSITORY, 'revision': REVISION,
                                     'quantization': 'Q8_0', **identity(model)})
    write_json(manifest_path, manifest)
    print('Weights installed.' if download_only else 'Installation complete; GPU validation has not run.', flush=True)
    print('Validate: uv run python scripts/install_decision2.py --verify', flush=True)
    print('Launch: uv run imajev-game --config configs/decision2-nox-4b-q8.yaml --debug-input', flush=True)
    return manifest


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


def verification_cases(config):
    from app.games.tic_tac_toe.game import TicTacToe
    game = TicTacToe()
    for label, moves in [('early', ['place_A1']),
                         ('tactical', ['place_A1', 'place_A2', 'place_B1', 'place_B2', 'place_C3'])]:
        state = game.initial_state()
        for move in moves:
            state = game.apply_action(state, move)
        request, _ = game.session_policy.prepare_decision(game, state, game.legal_actions(state), config, [], 0)
        yield label, game, state, request


def verify(root):
    from app.config import load_config
    from app.inference.imajev_client import ImajevClient
    from app.inference.opponents import create_opponent
    from app.inference.service_manager import ServiceManager

    active = active_inference(root)
    if active:
        raise RuntimeError(f'Close the running application/inference before verification (PIDs: {active}).')
    manifest_path = root / 'runtimes/decision2/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    validate_runtime(root / 'runtimes/decision2-src', manifest.get('runtime'))
    model = root / 'models/decision2-nox-4b' / FILENAME
    expected = manifest.get('model', {})
    actual = identity(model)
    if (actual['bytes'] != SIZE or actual['sha256'] != SHA256
            or expected.get('revision') != REVISION or any(expected.get(k) != v for k, v in actual.items())):
        raise ValueError('Installed Q8 weights do not match the pinned model manifest.')
    config = load_config(root / 'configs/decision2-nox-4b-q8.yaml')
    # The fork emits CUDA allocation/offloading evidence at trace verbosity.
    config = replace(config, opponent=replace(config.opponent_settings,
                     runtime_args=(*config.opponent_settings.runtime_args, '--log-verbosity', '4')))
    report_path = root / 'logs/decision2-verification.json'
    log_path = root / 'logs/decision2-verification.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger = logging.getLogger('app')
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    manager = ServiceManager(config)
    backend = create_opponent(config, manager=manager)
    report = {'version': 1, 'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'installation': manifest, 'cases': [], 'runtime_log': str(log_path)}
    sampler = GPUSampler()
    started = False
    native_log = root / 'logs/managed-opponent.log'
    offset = native_log.stat().st_size if native_log.exists() else 0
    try:
        sampler.start()
        started = True
        cases = list(verification_cases(config))
        def decide(case):
            label, game, state, request = case
            sampler.phase = 'nox_' + label
            before = time.monotonic()
            result = backend.choose_move(request)
            action = game.decode_decision(state, result)
            if result.probabilities is None or result.rejection:
                raise ValueError('Nox did not return a valid native choice distribution.')
            report['cases'].append({'name': label, 'seconds': time.monotonic() - before,
                                    'request': request, 'action': action, 'response': result.raw})
        for case in cases:
            decide(case)
        nox_pid = manager.opponent_process.pid
        sampler.phase = 'switch_to_imajev'
        manager.prepare_imajev(config)
        client = ImajevClient(config)
        client.manager = manager
        client.wait_ready()
        report['switch_to_imajev'] = {'previous_nox_pid': nox_pid, 'imajev_pid': manager.process.pid,
                                      'ready': True}
        decide(cases[0])
        report['switch_back_to_nox'] = {'nox_pid': manager.opponent_process.pid, 'ready': True}
        with native_log.open('rb') as stream:
            stream.seek(offset)
            log_text = stream.read().decode(errors='replace')
        matches = re.findall(r'offloaded\s+(\d+)/(\d+)\s+layers\s+to\s+GPU', log_text)
        if not matches or any(int(loaded) != int(total) or int(loaded) == 0 for loaded, total in matches):
            raise RuntimeError('Full GPU layer offloading was not confirmed in the Nox runtime log.')
        report['gpu_offloading'] = matches
        report['passed'] = True
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        sampler.phase = 'cleanup'
        try:
            manager.shutdown()
            report['cleanup_confirmed'] = True
        except BaseException as exc:
            report['passed'] = False
            report['cleanup_error'] = str(exc)
            raise
        finally:
            try:
                if started:
                    sampler.stop()
            except BaseException as exc:
                report['passed'] = False
                report['sampling_error'] = str(exc)
                raise
            finally:
                report['vram_samples'] = sampler.samples
                report['sampled_peak_vram_mib'] = max((gpu['used_mib'] for sample in sampler.samples
                                                      for gpu in sample['gpus']), default=None)
                # Keep the exact native runtime output from this run, including failures.
                if native_log.exists():
                    with native_log.open('rb') as stream:
                        stream.seek(offset)
                        report['native_runtime_output'] = stream.read().decode(errors='replace')
                write_json(report_path, report)
                logger.removeHandler(handler)
                logger.setLevel(old_level)
                handler.close()
    print(f'GPU decisions and ImaJEV/Nox switching verified. Report: {report_path}', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--download-only', action='store_true', help='Only install and verify pinned Q8 weights.')
    mode.add_argument('--verify', action='store_true', help='Test installed assets and GPU switching without rebuilding.')
    parser.add_argument('--cuda-arch', default='86', help='CUDA architecture (default: 86 for RTX 3070).')
    parser.add_argument('--jobs', type=int, default=4, help='Parallel build jobs (default: 4).')
    args = parser.parse_args()
    if args.jobs < 1 or not re.fullmatch(r'[0-9]+(?:;(?:[0-9]+))*', args.cuda_arch):
        parser.error('--jobs must be positive and --cuda-arch must contain numeric architectures separated by semicolons.')
    try:
        if args.verify:
            verify(ROOT)
        else:
            install(ROOT, args.download_only, args.cuda_arch, args.jobs)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, httpx.HTTPError) as exc:
        parser.exit(1, f'Decision 2.0 setup failed: {exc}\nSee logs/decision2-build.log or logs/decision2-verification.json when available.\n')


if __name__ == '__main__':
    main()
