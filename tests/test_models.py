from pathlib import Path
from contextlib import contextmanager
import hashlib
import subprocess
from types import SimpleNamespace

import pytest

from scripts import models


def write(root, name, contents='weights'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return path


def test_preview_then_cleanup_preserves_application_data(tmp_path):
    removed = [write(tmp_path, name) for name in (
        'models/liquid/model.gguf',
        '.inference/upstream/.cache/huggingface/hub/model/weights',
        '.inference/upstream/adapters/imajev-4b/adapter.safetensors',
        '.inference/upstream/artifacts/model-qwen4b.json',
        '.inference/runtime-manifest-4b-nf4.json',
    )]
    preserved = [write(tmp_path, name) for name in (
        'sessions/game.json', 'learning/coached-quoted/rules.json',
        'runtimes/decision3/manifest.json', 'inference/.venv/bin/python',
        '.inference/upstream/scripts/download_model.py',
        '.inference/upstream/artifacts/evaluation.json',
    )]
    models.clean(tmp_path)
    assert all(path.exists() for path in removed + preserved)
    models.clean(tmp_path, yes=True)
    assert not any(path.exists() for path in removed)
    assert all(path.exists() for path in preserved)
    models.clean(tmp_path, yes=True)  # Already empty is fine.


def test_cleanup_unlinks_weights_symlink_without_deleting_external_files(tmp_path):
    root = tmp_path / 'checkout'
    root.mkdir()
    external = write(tmp_path, 'external/model.gguf')
    (root / 'models').symlink_to(external.parent, target_is_directory=True)
    models.clean(root, yes=True)
    assert external.exists()
    assert not (root / 'models').is_symlink()


def test_redirected_inference_directory_is_rejected_before_deletion(tmp_path):
    root = tmp_path / 'checkout'
    root.mkdir()
    external = write(tmp_path, 'external/runtime-manifest.json')
    local = write(root, 'models/model.gguf')
    (root / '.inference').symlink_to(external.parent, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        models.clean(root, yes=True)
    assert local.exists()
    assert external.exists()


@pytest.mark.parametrize('default_only', [False, True])
def test_install_runs_selected_setup_with_local_cache(tmp_path, monkeypatch, default_only):
    calls = []
    monkeypatch.setenv('HF_HOME', '/external/cache')
    monkeypatch.setenv('HF_HUB_CACHE', '/another/cache')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    models.install(tmp_path, default_only)
    args, kwargs = calls[0]
    assert args[0] == ['bash', str(tmp_path / 'scripts/setup_inference.sh')]
    assert kwargs['check'] is True
    assert kwargs['cwd'] == tmp_path
    assert Path(kwargs['env']['HF_HOME']).is_relative_to(tmp_path)
    assert Path(kwargs['env']['HF_HUB_CACHE']).is_relative_to(tmp_path)
    if default_only:
        assert len(calls) == 1
    else:
        assert len(calls) == 4
        assert calls[1][0][0] == ['bash', str(tmp_path / 'scripts/install_llama_cpp.sh')]
        assert calls[2][0][0] == ['uv', 'run', '--locked', 'python', '-m', 'scripts.install_liquid_d1', '--with-model']
        assert calls[3][0][0] == ['uv', 'run', '--locked', 'python', '-m', 'scripts.install_decision3']


def test_liquid_weights_use_pinned_url_local_name_and_checksum(tmp_path, monkeypatch):
    from scripts import install_liquid_d1 as liquid
    data = b'Q8 weights fixture'
    downloads = []

    @contextmanager
    def stream(method, url, **kwargs):
        downloads.append(url)
        yield SimpleNamespace(raise_for_status=lambda: None, iter_bytes=lambda _: iter([data]))

    monkeypatch.setattr(liquid.httpx, 'stream', stream)
    sha256 = hashlib.sha256(data).hexdigest()
    path = liquid.install(tmp_path, liquid.MODEL_FILENAME, sha256, 'model.gguf')
    assert path == tmp_path / 'model.gguf'
    assert path.read_bytes() == data
    assert downloads == [f'https://huggingface.co/LiquidAI/d1-3B-GGUF/resolve/{liquid.REVISION}/{liquid.MODEL_FILENAME}']
    liquid.install(tmp_path, liquid.MODEL_FILENAME, sha256, 'model.gguf')
    assert len(downloads) == 1
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        liquid.install(tmp_path, liquid.MODEL_FILENAME, sha256, 'model.gguf')
    assert path.read_bytes() == b'corrupt'
