"""Install Decision 3.0 d3-mini Q8 and a separate official prebuilt CUDA runtime."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

import httpx
from scripts.inference_setup import digest, identity, write_json, output, active_inference, GPUSampler

ROOT = Path(__file__).resolve().parents[1]
RELEASE = 'b11606'
COMMIT = '1b70350c92563449576bcbbf552994b1d1ed847d'
MODEL_REPOSITORY = 'ggml-org/vllm-d3-mini-GGUF'
REVISION = 'c03ac9c3bdc5ae949111acb945338933f396e4d7'
MODEL = 'vllm-d3-mini-Q8_0.gguf'
PROJECTOR = 'mmproj-vllm-d3-mini-Q8_0.gguf'


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str
    size: int


def runtime_asset(name, sha256, size):
    return Asset(name, f'https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/{name}', sha256, size)


RUNTIME_ASSETS = (
    runtime_asset(f'llama-{RELEASE}-bin-ubuntu-cuda-13.4-x64.tar.gz',
                  'e984c5d442bf6310015440243a6f85927b320ecafccd50deaf9cc4a38daad22f', 152432083),
    runtime_asset(f'cudart-llama-{RELEASE}-bin-ubuntu-cuda-13.4-x64.tar.gz',
                  '29bde5b11418e49192e790d8fdc488715253bdf9cddd6736125907fa0f14405a', 440236707),
)
MODEL_ASSETS = (
    Asset(MODEL, f'https://huggingface.co/{MODEL_REPOSITORY}/resolve/{REVISION}/{MODEL}',
          '214d44cc351f084d8cc26dc85ce68af5a3c2e878c86c52e2be6c22312793f93d', 5157835200),
    Asset(PROJECTOR, f'https://huggingface.co/{MODEL_REPOSITORY}/resolve/{REVISION}/{PROJECTOR}',
          'bb79ba9f091df890684388f559f3145649007b5715d03760f99b767de41d9eda', 366894720),
)


def download(asset, directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / asset.name
    if destination.exists():
        if destination.stat().st_size != asset.size or digest(destination) != asset.sha256:
            raise ValueError(f'Existing asset differs from the pinned download: {destination}. Move it aside first.')
        print(f'Already verified: {asset.name}', flush=True)
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=asset.name + '.', suffix='.part', delete=False) as stream:
            temporary = Path(stream.name)
            checksum = hashlib.sha256()
            received = progress = 0
            print(f'Downloading {asset.name} ({asset.size / 1e9:.2f} GB)', flush=True)
            with httpx.stream('GET', asset.url, follow_redirects=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 1024):
                    stream.write(chunk)
                    checksum.update(chunk)
                    received += len(chunk)
                    if received - progress >= 256 * 1024 * 1024:
                        print(f'  {received / 1e9:.2f} / {asset.size / 1e9:.2f} GB', flush=True)
                        progress = received
            stream.flush()
            os.fsync(stream.fileno())
        if received != asset.size or checksum.hexdigest() != asset.sha256:
            raise ValueError(f'Asset size or SHA-256 differs: {asset.name}')
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def runtime_files(prefix):
    return [{'relative_path': str(path.relative_to(prefix)), 'bytes': path.stat().st_size,
             'sha256': digest(path)} for path in sorted(prefix.rglob('*')) if path.is_file()]


def validate_runtime(root, runtime):
    prefix = root / 'runtimes/decision3/install'
    if not runtime or runtime.get('release') != RELEASE or runtime.get('commit') != COMMIT or runtime.get('backend') != 'cuda':
        raise ValueError('Missing or incompatible Decision3 runtime manifest. Run the installer.')
    if runtime.get('files') != runtime_files(prefix) or not runtime.get('files'):
        raise ValueError('Installed Decision3 runtime files differ from the manifest. Move the installation aside first.')
    executable = prefix / 'llama-server'
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('Decision3 llama-server is missing or not executable.')
    return executable


def extract_bundle(archive, stage):
    # Official bundles have one enclosing directory. Python's data filter rejects
    # traversal, absolute link targets and special files before flattening it.
    with tempfile.TemporaryDirectory(dir=stage.parent, prefix='bundle-') as temporary:
        unpacked = Path(temporary)
        with tarfile.open(archive, 'r:gz') as bundle:
            bundle.extractall(unpacked, filter='data')
        entries = list(unpacked.iterdir())
        if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
            raise ValueError('Unexpected runtime bundle layout.')
        for path in entries[0].rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(entries[0].resolve()):
                raise ValueError('Runtime bundle link escapes its installation directory.')
        shutil.copytree(entries[0], stage, symlinks=True, dirs_exist_ok=True)


def install_runtime(root, previous):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        raise RuntimeError('The pinned Decision3 CUDA bundle requires Linux/WSL2 x86_64.')
    runtime = root / 'runtimes/decision3'
    prefix = runtime / 'install'
    if previous:
        validate_runtime(root, previous)
        print('Pinned Decision3 CUDA runtime already verified.', flush=True)
        return previous
    if prefix.exists():
        raise ValueError('Existing Decision3 runtime has no matching manifest. Move it aside before installing.')
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime, prefix='install-') as temporary:
        stage = Path(temporary) / 'contents'
        stage.mkdir()
        for asset in RUNTIME_ASSETS:
            archive = download(asset, runtime)
            extract_bundle(archive, stage)
        executable = stage / 'llama-server'
        version = output([executable, '--version'])
        if COMMIT[:7] not in version:
            raise ValueError('Downloaded runtime version does not match the pinned commit.')
        files = runtime_files(stage)
        os.replace(stage, prefix)
    result = {'repository': 'https://github.com/ggml-org/llama.cpp', 'release': RELEASE, 'commit': COMMIT,
              'backend': 'cuda', 'cuda_bundle': '13.4', 'version': version, 'files': files,
              'assets': [{'name': a.name, 'url': a.url, 'sha256': a.sha256, 'bytes': a.size} for a in RUNTIME_ASSETS]}
    return result


def install(root, download_only=False):
    manifest_path = root / 'runtimes/decision3/manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not download_only:
        manifest['runtime'] = install_runtime(root, manifest.get('runtime'))
        write_json(manifest_path, manifest)
        # Only discard installer-owned downloads after the published runtime and
        # its manifest exist; installed libraries remain in the private prefix.
        for asset in RUNTIME_ASSETS:
            (manifest_path.parent / asset.name).unlink(missing_ok=True)
    for key, asset in zip(('model', 'projector'), MODEL_ASSETS):
        path = download(asset, root / 'models/decision3-mini-4b')
        manifest[key] = {'repository': MODEL_REPOSITORY, 'revision': REVISION, 'quantization': 'Q8_0', **identity(path)}
        write_json(manifest_path, manifest)
    manifest.update(version=1)
    write_json(manifest_path, manifest)
    print('Decision3 weights installed.' if download_only else 'Decision3 installation complete; GPU validation has not run.', flush=True)
    print('Verify: uv run python scripts/install_decision3.py --verify', flush=True)
    print('Launch: uv run imajev-game --config configs/decision3-mini-4b-q8.yaml --debug-input', flush=True)
    return manifest


def validate_installation(root):
    path = root / 'runtimes/decision3/manifest.json'
    if not path.exists():
        raise ValueError('Decision3 is not installed. Run scripts/install_decision3.py first.')
    manifest = json.loads(path.read_text())
    validate_runtime(root, manifest.get('runtime'))
    for key, asset in zip(('model', 'projector'), MODEL_ASSETS):
        model = root / 'models/decision3-mini-4b' / asset.name
        expected = manifest.get(key, {})
        if (not model.is_file() or model.stat().st_size != asset.size or digest(model) != asset.sha256
                or expected.get('revision') != REVISION or expected.get('sha256') != asset.sha256):
            raise ValueError(f'Decision3 {key} differs from its pinned installation.')
    return manifest


def verify(root):
    from dataclasses import replace
    from PyQt6.QtGui import QGuiApplication
    from app.config import load_config
    from app.core.contracts import Stroke
    from app.games.tic_tac_toe.game import TicTacToe
    from app.games.boku.game import Boku
    from app.inference.opponents import create_opponent
    from app.inference.recognition import create_recognizer
    from app.inference.service_manager import ServiceManager
    from app.ui.rendering import observation_png
    from scripts.validate_recognition import cases as recognition_cases

    active = active_inference(root)
    if active:
        raise RuntimeError(f'Close the running application/inference before verification (PIDs: {active}).')
    manifest = validate_installation(root)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    application = QGuiApplication.instance() or QGuiApplication([])
    config = load_config(root / 'configs/decision3-mini-4b-q8.yaml')
    config = replace(config, opponent=replace(config.opponent_settings,
                     runtime_args=(*config.opponent_settings.runtime_args, '--log-verbosity', '4')))
    manager = ServiceManager(config)
    opponent = create_opponent(config, manager=manager)
    sampler = GPUSampler()
    report = {'version': 1, 'started_at': datetime.now(timezone.utc).isoformat(), 'installation': manifest,
              'integration_passed': False, 'cases': [], 'recognition_controls': [],
              'playing_strength': 'unassessed; integration checks are not a strategy benchmark'}
    native_log = root / 'logs/managed-opponent.log'
    offset = native_log.stat().st_size if native_log.exists() else 0
    def decide(game, state, backend, label):
        request, _ = game.session_policy.prepare_decision(game, state, game.legal_actions(state), config, [], 0)
        before = time.monotonic()
        result = backend.choose_move(request)
        action = game.decode_decision(state, result)
        if result.probabilities is None:
            raise ValueError('Decision3 returned no valid native choice distribution.')
        row = {'name': label, 'action': action, 'seconds': time.monotonic()-before, 'request': request, 'response': result.raw}
        report['cases'].append(row)
        return row
    started = False
    try:
        sampler.start(); started = True
        game = TicTacToe()
        state = game.apply_action(game.initial_state(), 'place_A1')
        sampler.phase = 'decision3_text'
        decide(game, state, opponent, 'early_ttt')
        tactical = game.initial_state()
        for move in ('place_A1', 'place_A2', 'place_B1', 'place_B2', 'place_C3'):
            tactical = game.apply_action(tactical, move)
        decide(game, tactical, opponent, 'tactical_ttt')
        # Exercise the complete Boku option set; no filtering to a 26-choice subset.
        boku = Boku()
        boku_state = boku.apply_action(boku.initial_state(), 'place_F5')
        decide(boku, boku_state, opponent, 'early_boku')
        previous_pid = manager.opponent_process.pid
        sampler.phase = 'liquid_recognition'
        liquid = create_recognizer(config, manager=manager)
        ink = (Stroke(((.08,.08),(.25,.25))), Stroke(((.25,.08),(.08,.25))))
        blank = game.initial_state()
        image = observation_png(game.render(blank, ink, 'recognition'), config.observation_size)
        before = time.monotonic()
        reply = liquid.recognize(game.recognition_request(blank, ink), image)
        accepted = game.decode_recognition(blank, ink, reply, config.threshold)
        report['liquid_switch'] = {'previous_decision3_pid': previous_pid, 'liquid_pid': manager.opponent_process.pid,
                                  'action': accepted, 'seconds': time.monotonic()-before}
        sampler.phase = 'decision3_return'
        decide(game, state, opponent, 'after_liquid_switch')
        # The shared profile keeps the projector loaded for both recognition and
        # text moves; runtime identity must remain equal across those roles.
        shared = load_config(root / 'configs/decision3-mini-4b-shared.yaml')
        recognizer = create_recognizer(shared, manager=manager)
        sampler.phase = 'decision3_vision'
        pid = None
        for name, g, s, drawing, expected in recognition_cases():
            response = recognizer.recognize(g.recognition_request(s, drawing),
                         observation_png(g.render(s, drawing, 'recognition'), shared.observation_size))
            if pid is None:
                pid = manager.opponent_process.pid
            elif pid != manager.opponent_process.pid:
                raise RuntimeError('Decision3 vision controls failed to reuse their runtime.')
            action = rejection = None
            try: action = g.decode_recognition(s, drawing, response, shared.threshold)
            except ValueError as exc: rejection = str(exc)
            report['recognition_controls'].append({'name': name, 'expected': expected, 'accepted': action,
                                                  'rejection': rejection, 'response': response.raw})
        shared_opponent = create_opponent(shared, manager=manager)
        decide(game, state, shared_opponent, 'shared_text_after_vision')
        if manager.opponent_process.pid != pid:
            raise RuntimeError('Shared Decision3 recognition/opponent failed to reuse its runtime.')
        report['shared_runtime_reused'] = True
        report['shared_recognition_controls_passed'] = all(c['accepted'] == c['expected'] for c in report['recognition_controls'])
        with native_log.open('rb') as stream:
            stream.seek(offset)
            log_text = stream.read().decode(errors='replace')
        matches = re.findall(r'offloaded\s+(\d+)/(\d+)\s+layers\s+to\s+GPU', log_text)
        if not matches or any(int(loaded) != int(total) or int(loaded) == 0 for loaded, total in matches):
            raise RuntimeError('Full Decision3 GPU layer offloading was not confirmed.')
        report['gpu_offloading'] = matches
        report['integration_passed'] = True
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        try:
            manager.shutdown()
            report['cleanup_confirmed'] = True
        except BaseException as exc:
            report['integration_passed'] = False
            report['cleanup_error'] = str(exc)
            raise
        finally:
            try:
                if started: sampler.stop()
            except BaseException as exc:
                report['integration_passed'] = False
                report['sampling_error'] = str(exc)
                raise
            finally:
                report['vram_samples'] = sampler.samples
                report['sampled_peak_vram_mib'] = max((gpu['used_mib'] for sample in sampler.samples for gpu in sample['gpus']), default=None)
                if native_log.exists():
                    with native_log.open('rb') as stream:
                        stream.seek(offset)
                        report['native_runtime_output'] = stream.read().decode(errors='replace')
                write_json(root / 'logs/decision3-verification.json', report)
    print(f'Decision3 GPU integration passed. Shared recognition controls passed: {report["shared_recognition_controls_passed"]}.', flush=True)
    print('Report: logs/decision3-verification.json. Playing strength is unassessed.', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--download-only', action='store_true', help='Install Q8 weights and projector without a runtime.')
    mode.add_argument('--verify', action='store_true', help='Validate the installed GPU runtime, switching and vision controls.')
    args = parser.parse_args()
    try:
        if args.verify: verify(ROOT)
        else: install(ROOT, args.download_only)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.SubprocessError, httpx.HTTPError) as exc:
        print(f'Decision3 setup failed: {exc}', flush=True)
        print('See logs/decision3-verification.json and logs/managed-opponent.log when available.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
