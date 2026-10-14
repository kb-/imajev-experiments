import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace

import httpx
import pytest

from app.config import load_config
from app.inference.service_manager import runtime_key
from scripts import install_decision3 as installer


def archive_bytes(files, links=()):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith('llama-server') else 0o644
            tar.addfile(member, io.BytesIO(data))
        for name, target in links:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            tar.addfile(member)
    return buffer.getvalue()


def mock_downloads(monkeypatch, contents):
    calls = []
    class Response:
        def __init__(self, url): self.url = url
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def raise_for_status(self): pass
        def iter_bytes(self, _): yield contents[self.url]
    def stream(method, url, **kwargs):
        assert method == 'GET'
        calls.append(url)
        return Response(url)
    monkeypatch.setattr(installer.httpx, 'stream', stream)
    return calls


@pytest.fixture
def assets(monkeypatch):
    blobs = [archive_bytes({'build/llama-server': b'CUDA server', 'build/libggml-cuda.so.0': b'CUDA backend'},
                           [('build/libggml-cuda.so', 'libggml-cuda.so.0')]),
             archive_bytes({'lib/libcudart.so.13': b'CUDA library'}), b'Q8 model', b'Q8 projector']
    names = ['server.tar.gz', 'libraries.tar.gz', installer.MODEL, installer.PROJECTOR]
    specs = [installer.Asset(name, 'https://example.invalid/' + name, hashlib.sha256(blob).hexdigest(), len(blob))
             for name, blob in zip(names, blobs)]
    monkeypatch.setattr(installer, 'RUNTIME_ASSETS', tuple(specs[:2]))
    monkeypatch.setattr(installer, 'MODEL_ASSETS', tuple(specs[2:]))
    monkeypatch.setattr(installer.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(installer.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(installer, 'output', lambda *_: 'CUDA runtime ' + installer.COMMIT[:9])
    calls = mock_downloads(monkeypatch, {spec.url: blob for spec, blob in zip(specs, blobs)})
    return specs, calls


def test_full_install_is_isolated_and_repeatable(tmp_path, assets):
    specs, calls = assets
    liquid = tmp_path / 'runtimes/llama_cpp/install/llama-server'
    liquid.parent.mkdir(parents=True)
    liquid.write_bytes(b'working Liquid runtime')
    legacy = tmp_path / 'models/decision2-nox-4b/old.gguf'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'legacy baseline')
    first = installer.install(tmp_path)
    assert len(calls) == 4
    assert installer.validate_installation(tmp_path) == first
    assert installer.install(tmp_path) == first
    assert len(calls) == 4
    assert liquid.read_bytes() == b'working Liquid runtime'
    assert legacy.read_bytes() == b'legacy baseline'
    assert not list((tmp_path / 'runtimes/decision3').glob('*.tar.gz'))
    assert (tmp_path / 'runtimes/decision3/install/libggml-cuda.so').is_symlink()
    assert first['model']['sha256'] == specs[2].sha256
    assert first['projector']['revision'] == installer.REVISION
    assert first['runtime']['release'] == installer.RELEASE


def test_download_only_needs_no_runtime_or_compiler(tmp_path, monkeypatch, assets):
    monkeypatch.setattr(installer, 'install_runtime', lambda *_: pytest.fail('runtime attempted'))
    report = installer.install(tmp_path, download_only=True)
    assert 'runtime' not in report
    assert report['model']['quantization'] == 'Q8_0'
    assert not (tmp_path / 'runtimes/decision3/install').exists()
    with pytest.raises(ValueError, match='runtime manifest'):
        installer.validate_installation(tmp_path)


def test_checksum_failure_never_installs_weights(tmp_path, monkeypatch, assets):
    specs, _ = assets
    mock_downloads(monkeypatch, {specs[2].url: b'corrupt'})
    with pytest.raises(ValueError, match='SHA-256'):
        installer.download(specs[2], tmp_path)
    assert not list(tmp_path.iterdir())


def test_interrupted_download_cleans_partial(tmp_path, monkeypatch, assets):
    specs, _ = assets
    def fail(*_, **__): raise httpx.ConnectError('offline')
    monkeypatch.setattr(installer.httpx, 'stream', fail)
    with pytest.raises(httpx.ConnectError): installer.download(specs[2], tmp_path)
    assert not list(tmp_path.iterdir())


def test_model_conflict_is_not_overwritten(tmp_path, assets):
    specs, calls = assets
    path = tmp_path / specs[2].name
    path.write_bytes(b'custom model')
    with pytest.raises(ValueError, match='Move it aside'):
        installer.download(specs[2], tmp_path)
    assert path.read_bytes() == b'custom model' and not calls


def test_runtime_version_failure_keeps_installation_unpublished(tmp_path, monkeypatch, assets):
    monkeypatch.setattr(installer, 'output', lambda *_: 'wrong runtime')
    with pytest.raises(ValueError, match='pinned commit'):
        installer.install(tmp_path)
    assert not (tmp_path / 'runtimes/decision3/install').exists()
    assert not (tmp_path / 'runtimes/decision3/manifest.json').exists()
    assert not (tmp_path / 'models').exists()


def test_modified_runtime_is_preserved_and_rejected(tmp_path, assets):
    installer.install(tmp_path)
    executable = tmp_path / 'runtimes/decision3/install/llama-server'
    executable.write_bytes(b'changed')
    with pytest.raises(ValueError, match='differ'):
        installer.install(tmp_path)
    assert executable.read_bytes() == b'changed'


def test_runtime_without_manifest_is_not_replaced(tmp_path, assets):
    prefix = tmp_path / 'runtimes/decision3/install'
    prefix.mkdir(parents=True)
    marker = prefix / 'custom'
    marker.write_text('custom runtime')
    with pytest.raises(ValueError, match='no matching manifest'):
        installer.install(tmp_path)
    assert marker.read_text() == 'custom runtime'


@pytest.mark.parametrize('name,target', [('bundle/link', '../outside'), ('bundle/link', '/etc/passwd')])
def test_bundle_links_cannot_escape_prefix(tmp_path, name, target):
    path = tmp_path / 'bundle.tar.gz'
    path.write_bytes(archive_bytes({'bundle/llama-server': b'executable'}, [(name, target)]))
    stage = tmp_path / 'stage'
    stage.mkdir()
    with pytest.raises((ValueError, tarfile.TarError)):
        installer.extract_bundle(path, stage)
    assert not list(stage.iterdir())


def test_verification_refuses_running_app_before_loading(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'active_inference', lambda _: [123])
    monkeypatch.setattr(installer, 'validate_installation', lambda _: pytest.fail('assets accessed before exclusivity check'))
    with pytest.raises(RuntimeError, match='PIDs:.*123'):
        installer.verify(tmp_path)


def test_profiles_use_isolated_runtime_and_shared_resource_identity():
    config = load_config(Path('configs/decision3-mini-4b-q8.yaml'))
    shared = load_config(Path('configs/decision3-mini-4b-shared.yaml'))
    assert config.recognition_settings.model == 'liquid-d1-3b-q8'
    assert config.opponent_settings.model == 'decision3-mini-4b-q8'
    assert config.opponent_settings.model_path.endswith(installer.MODEL)
    assert config.opponent_settings.executable.endswith('/runtimes/decision3/install/llama-server')
    assert not config.tactical_guard and config.move_temperature == 0
    assert shared.recognition_settings.input_mode == 'text_image'
    assert shared.opponent_settings.input_mode == 'text'
    assert runtime_key(shared.recognition_settings) == runtime_key(shared.opponent_settings)
    assert shared.recognition_fallback.model == 'imajev-4b-nf4'
