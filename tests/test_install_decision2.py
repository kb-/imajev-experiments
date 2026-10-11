import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from app.config import load_config
from app.inference.opponents import parse_move
from scripts import install_decision2 as installer


@pytest.fixture
def asset(monkeypatch):
    data = b'GGUF pinned Q8 test weights'
    monkeypatch.setattr(installer, 'SIZE', len(data))
    monkeypatch.setattr(installer, 'SHA256', hashlib.sha256(data).hexdigest())
    return data


@pytest.fixture
def runtime_patch(monkeypatch):
    original = 'switch (type) {\n        case COMMON_DECISION_TYPE_OPENJEV:\n            return {"decisions"};\n}\n'
    patched = original.replace('        case COMMON_DECISION_TYPE_OPENJEV:\n',
                               '        case COMMON_DECISION_TYPE_OPENJEV:\n        case COMMON_DECISION_TYPE_DECISION2:\n')
    monkeypatch.setattr(installer, 'PATCH_ORIGINAL_SHA256', hashlib.sha256(original.encode()).hexdigest())
    monkeypatch.setattr(installer, 'PATCH_SHA256', hashlib.sha256(patched.encode()).hexdigest())
    monkeypatch.setattr(installer, 'PATCHES', [{'name': 'test-patch', 'file': installer.PATCH_FILE,
                                             'sha256': installer.PATCH_SHA256}])
    return original, patched


def transport(monkeypatch, data, fail=False):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def raise_for_status(self):
            if fail:
                raise httpx.ConnectError('connection failed')
        def iter_bytes(self, _): yield data
    def stream(method, url, **kwargs):
        calls.append((method, url))
        return Response()
    monkeypatch.setattr(installer.httpx, 'stream', stream)
    return calls


def test_missing_prerequisites_prevent_build_and_download(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, 'which', lambda name: None if name == 'nvcc' else '/bin/' + name)
    monkeypatch.setattr(installer, 'build', lambda *a: pytest.fail('build attempted'))
    monkeypatch.setattr(installer, 'download', lambda *a: pytest.fail('download attempted'))
    with pytest.raises(RuntimeError, match='CUDA nvcc') as error:
        installer.install(tmp_path)
    assert 'Windows NVIDIA driver' in str(error.value)
    assert not (tmp_path / 'runtimes').exists()


def test_pinned_download_atomic_and_repeat_install(tmp_path, monkeypatch, asset):
    calls = transport(monkeypatch, asset)
    path = installer.download(tmp_path)
    assert path.read_bytes() == asset
    assert calls == [('GET', installer.URL)]
    assert installer.REVISION in calls[0][1]
    assert installer.download(tmp_path) == path and len(calls) == 1
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('bad', [b'corrupt', b'GGUF pinned Q8 test weightz'])
def test_corrupt_download_never_publishes_asset(tmp_path, monkeypatch, asset, bad):
    transport(monkeypatch, bad)
    with pytest.raises(ValueError, match='SHA-256'):
        installer.download(tmp_path)
    assert not list(tmp_path.iterdir())


def test_interrupted_download_cleans_temporary_file(tmp_path, monkeypatch, asset):
    transport(monkeypatch, asset, fail=True)
    with pytest.raises(httpx.ConnectError):
        installer.download(tmp_path)
    assert not list(tmp_path.iterdir())


def test_conflicting_existing_weights_are_preserved(tmp_path, monkeypatch, asset):
    path = tmp_path / installer.FILENAME
    path.write_bytes(b'user weights')
    monkeypatch.setattr(installer.httpx, 'stream', lambda *a, **k: pytest.fail('download attempted'))
    with pytest.raises(ValueError, match='Move it aside'):
        installer.download(tmp_path)
    assert path.read_bytes() == b'user weights'


def test_download_only_needs_no_compiler_and_keeps_runtime_manifest(tmp_path, monkeypatch, asset):
    transport(monkeypatch, asset)
    monkeypatch.setattr(installer, 'prerequisites', lambda: pytest.fail('compiler required'))
    path = tmp_path / 'runtimes/decision2/manifest.json'
    installer.write_json(path, {'runtime': {'existing': True}})
    result = installer.install(tmp_path, download_only=True)
    assert result['model']['sha256'] == installer.SHA256
    assert result['model']['revision'] == installer.REVISION
    assert result['runtime'] == {'existing': True}


@pytest.mark.parametrize('head,status', [('other', ''), (installer.COMMIT, ' M src/model.cpp')])
def test_conflicting_checkout_is_not_modified(tmp_path, monkeypatch, head, status):
    source = tmp_path / 'runtimes/decision2-src'
    (source / '.git').mkdir(parents=True)
    monkeypatch.setattr(installer, 'output', lambda cmd: head if 'rev-parse' in cmd else status)
    monkeypatch.setattr(installer, 'run', lambda *a, **k: pytest.fail('checkout modified'))
    with pytest.raises(ValueError):
        installer.build(tmp_path, {}, '86', 4, None)


def test_pinned_cuda_build_and_repeat_verification(tmp_path, monkeypatch, runtime_patch):
    trace = []
    source = tmp_path / 'runtimes/decision2-src'
    executable = source / 'build/bin/llama-server'
    tools = {'CMake': 'cmake', 'CUDA nvcc': '/cuda/bin/nvcc', 'C++ compiler': 'c++'}
    def run(command, **kwargs):
        trace.append(list(map(str, command)))
        if command[:2] == ['git', 'init']:
            (source / '.git').mkdir(parents=True)
            path = source / installer.PATCH_FILE
            path.parent.mkdir(parents=True)
            path.write_text(runtime_patch[0])
        if '--build' in command:
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b'pinned CUDA executable')
            (executable.parent / 'libggml-cuda.so').write_bytes(b'CUDA library')
    monkeypatch.setattr(installer, 'run', run)
    monkeypatch.setattr(installer, 'output', lambda cmd: installer.COMMIT if 'rev-parse' in cmd else
                        '' if 'status' in cmd else 'CUDA build version')
    result = installer.build(tmp_path, tools, '86', 4, None)
    assert any(installer.COMMIT in command and 'fetch' in command for command in trace)
    configure = next(command for command in trace if '-S' in command)
    assert '-DGGML_CUDA=ON' in configure and '-DCMAKE_CUDA_ARCHITECTURES=86' in configure
    assert trace[-1][-2:] == ['--parallel', '4']
    assert result['cuda_compiler'] == 'CUDA build version'
    assert result['libraries'][0]['sha256'] == installer.digest(executable.parent / 'libggml-cuda.so')
    trace.clear()
    assert installer.build(tmp_path, tools, '86', 4, result) == result
    assert trace == []
    # A previously installed unpatched runtime upgrades incrementally, retaining CUDA objects.
    legacy = {key: value for key, value in result.items() if key != 'patches'}
    (source / installer.PATCH_FILE).write_text(runtime_patch[0])
    upgraded = installer.build(tmp_path, tools, '86', 16, legacy)
    assert upgraded['patches'] == installer.PATCHES
    assert trace[-1][-2:] == ['--parallel', '16']
    executable.write_bytes(b'changed executable')
    with pytest.raises(ValueError, match='differs'):
        installer.build(tmp_path, tools, '86', 4, result)


def test_checkout_accepts_only_exact_recorded_patch(tmp_path, monkeypatch, runtime_patch):
    (tmp_path / '.git').mkdir()
    path = tmp_path / installer.PATCH_FILE
    path.parent.mkdir(parents=True)
    path.write_text(runtime_patch[0])
    installer.patch_runtime(tmp_path)
    assert path.read_text() == runtime_patch[1]
    installer.patch_runtime(tmp_path)  # Idempotent.
    monkeypatch.setattr(installer, 'output', lambda cmd: installer.COMMIT if 'rev-parse' in cmd else
                        ' M ' + installer.PATCH_FILE)
    installer.check_checkout(tmp_path)
    path.write_text(runtime_patch[1] + '// another edit\n')
    with pytest.raises(ValueError, match='modified'):
        installer.check_checkout(tmp_path)


def test_build_failure_does_not_download_or_claim_success(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'prerequisites', lambda: {})
    def build(*args): raise subprocess.CalledProcessError(1, ['cmake'])
    monkeypatch.setattr(installer, 'build', build)
    monkeypatch.setattr(installer, 'download', lambda *a: pytest.fail('download after failed build'))
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(tmp_path)
    assert not (tmp_path / 'runtimes/decision2/manifest.json').exists()


def test_q8_profile_and_gameplay_requests():
    config = load_config(Path('configs/decision2-nox-4b-q8.yaml'))
    settings = config.opponent_settings
    assert settings.model == 'decision2-nox-4b-q8'
    assert settings.context_limit == 2048 and settings.max_choices == 255
    assert Path(settings.model_path).name == installer.FILENAME
    assert settings.runtime_args == ('-ngl', '99', '--embeddings', '--pooling', 'none', '-b', '1024', '-ub', '1024')
    cases = list(installer.verification_cases(config))
    assert len(cases[0][3]['questions']['move']['criteria']) == 8
    from app.games.tic_tac_toe.prompting import winning_cells
    assert winning_cells(cases[1][2].board, 'O')
    for _, game, state, request in cases:
        offered = request['questions']['move']['criteria']
        assert len(offered) == len(game.legal_actions(state))
        raw = {'model': settings.model, 'answers': {'move': {'type': 'choice', 'error': 'invalid_question'}}}
        result = parse_move(raw, request, settings)
        assert result.raw is raw and result.rejection == 'Decision question move: invalid_question'
        with pytest.raises(ValueError, match='invalid_question'):
            game.decode_decision(state, result)


def test_verify_refuses_active_application_before_touching_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'active_inference', lambda _: [123])
    with pytest.raises(RuntimeError, match='PIDs:.*123'):
        installer.verify(tmp_path)


def test_verify_requires_installed_cuda_runtime(tmp_path):
    with pytest.raises(RuntimeError, match='after installing nvcc'):
        installer.validate_runtime(tmp_path / 'missing-runtime', None)


def test_active_inference_detects_real_checkout_app(tmp_path):
    # A child with app.main in argv is enough; it never imports or starts the GUI.
    import sys
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', '-m', 'app.main'],
                             cwd=tmp_path)
    try:
        assert child.pid in installer.active_inference(tmp_path)
        assert child.pid not in installer.active_inference(tmp_path / 'other-checkout')
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize('failure', [None, 'capacity', 'cleanup', 'sampling', 'cpu'])
def test_verify_switches_in_order_and_saves_failures(tmp_path, monkeypatch, asset, failure):
    from app.inference.opponents import parse_move
    config = load_config(Path('configs/decision2-nox-4b-q8.yaml'))
    monkeypatch.setattr('app.config.load_config', lambda _: config)
    monkeypatch.setattr(installer, 'active_inference', lambda _: [])
    monkeypatch.setattr(installer, 'validate_runtime', lambda *_: None)
    model = tmp_path / 'models/decision2-nox-4b' / installer.FILENAME
    model.parent.mkdir(parents=True)
    model.write_bytes(asset)
    installer.write_json(tmp_path / 'runtimes/decision2/manifest.json',
                         {'runtime': {}, 'model': {'revision': installer.REVISION, **installer.identity(model)}})
    trace = []
    class Manager:
        def __init__(self, _):
            self.process = self.opponent_process = None
        def prepare_imajev(self, _):
            trace.append('nox_exit_then_imajev')
            self.opponent_process = None
            self.process = SimpleNamespace(pid=200)
        def shutdown(self):
            trace.append('shutdown')
            if failure == 'cleanup': raise RuntimeError('unconfirmed cleanup')
    class Backend:
        def __init__(self, manager): self.manager = manager
        def choose_move(self, request):
            trace.append('imajev_exit_then_nox' if self.manager.process else 'nox')
            self.manager.process = None
            self.manager.opponent_process = SimpleNamespace(pid=100)
            if failure == 'capacity': raise RuntimeError('prompt exceeds physical batch')
            log = tmp_path / 'logs/managed-opponent.log'
            log.write_text('offloaded 0/33 layers to GPU' if failure == 'cpu' else 'offloaded 33/33 layers to GPU')
            keys = list(request['questions']['move']['criteria'])
            raw = {'model': config.opponent_settings.model, 'answers': {'move': {
                'type': 'choice', 'choice': keys[0], 'probabilities': {k: 1 / len(keys) for k in keys}}}}
            return parse_move(raw, request, config.opponent_settings)
    class Sampler:
        samples = [{'gpus': [{'used_mib': 4800}]}]
        def start(self): trace.append('sample')
        def stop(self):
            trace.append('stop_sample')
            if failure == 'sampling': raise RuntimeError('nvidia-smi failed')
    monkeypatch.setattr(installer, 'GPUSampler', Sampler)
    monkeypatch.setattr('app.inference.service_manager.ServiceManager', Manager)
    monkeypatch.setattr('app.inference.opponents.create_opponent', lambda _, manager: Backend(manager))
    monkeypatch.setattr('app.inference.imajev_client.ImajevClient', lambda _: SimpleNamespace(
        wait_ready=lambda: trace.append('imajev_ready')))
    if failure:
        with pytest.raises(RuntimeError): installer.verify(tmp_path)
    else:
        assert installer.verify(tmp_path)['passed']
        assert trace == ['sample', 'nox', 'nox', 'nox_exit_then_imajev', 'imajev_ready',
                         'imajev_exit_then_nox', 'shutdown', 'stop_sample']
    report = json.loads((tmp_path / 'logs/decision2-verification.json').read_text())
    assert report['passed'] == (failure is None)
    assert report['sampled_peak_vram_mib'] == 4800
    assert 'shutdown' in trace and 'stop_sample' in trace
