import pytest

from app.inference.cuda_environment import inference_environment


def bundle(root, name):
    path = root / 'drivers' / name / 'libnvidia-ptxjitcompiler.so.1'
    path.parent.mkdir(parents=True)
    path.touch()
    return path


def shim(root):
    path = root / 'lib/libcuda.so.1'
    path.parent.mkdir(parents=True)
    path.touch()


def test_non_wsl_environment_unchanged(tmp_path):
    original = {'PATH': '/bin', 'HF_HUB_OFFLINE': '1'}
    result = inference_environment(original, tmp_path)
    assert result == original and result is not original


def test_preloads_wsl_compiler_only_in_child_environment(tmp_path):
    shim(tmp_path)
    compiler = bundle(tmp_path, 'current')
    original = {'LD_PRELOAD': '/other.so', 'HF_HUB_OFFLINE': '1'}
    result = inference_environment(original, tmp_path)
    assert result == dict(original, LD_PRELOAD=f'{compiler}:/other.so')
    assert original['LD_PRELOAD'] == '/other.so'


def test_respects_explicit_compiler(tmp_path):
    shim(tmp_path)
    bundle(tmp_path, 'current')
    original = {'LD_PRELOAD': '/selected/libnvidia-ptxjitcompiler.so.1'}
    assert inference_environment(original, tmp_path) == original


def test_selects_active_driver_among_old_windows_bundles(tmp_path, monkeypatch):
    shim(tmp_path)
    bundle(tmp_path, 'old')
    current = bundle(tmp_path, 'current')
    def probe(command, **kwargs):
        assert command[-1] == str(tmp_path / 'lib/libcuda.so.1')
        assert kwargs['timeout'] == 15
        return f'0-1 r-xp 0 00:00 0 {current.parent}/libcuda.so.1.1\n'
    monkeypatch.setattr('app.inference.cuda_environment.subprocess.check_output', probe)
    assert inference_environment({}, tmp_path)['LD_PRELOAD'] == str(current)


def test_never_guesses_between_unidentified_drivers(tmp_path, monkeypatch):
    shim(tmp_path)
    bundle(tmp_path, 'old')
    bundle(tmp_path, 'current')
    monkeypatch.setattr('app.inference.cuda_environment.subprocess.check_output', lambda *a, **k: '')
    with pytest.raises(RuntimeError, match='Cannot identify'):
        inference_environment({}, tmp_path)
