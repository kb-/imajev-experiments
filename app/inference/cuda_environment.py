"""Keep managed WSL inference on the Windows driver's CUDA libraries."""
import logging
import os
from pathlib import Path
import subprocess
import sys

logger = logging.getLogger(__name__)


def inference_environment(environment=None, wsl_root=Path('/usr/lib/wsl')):
    """Return a child-only environment; never change system libraries or os.environ.

    Ubuntu's CUDA toolkit can install a Linux PTX compiler alongside the WSL
    driver. CUDA 13 may load that incompatible compiler even when libcuda itself
    comes from WSL. Preload the PTX compiler from the same Windows driver bundle.
    """
    env = dict(os.environ if environment is None else environment)
    wsl_root = Path(wsl_root)
    shim = wsl_root / 'lib/libcuda.so.1'
    candidates = list((wsl_root / 'drivers').glob('*/libnvidia-ptxjitcompiler.so.1'))
    if not shim.is_file() or not candidates:
        return env
    # Respect an explicitly selected compiler, including a caller's workaround.
    if 'libnvidia-ptxjitcompiler' in env.get('LD_PRELOAD', ''):
        return env
    if len(candidates) == 1:
        compiler = candidates[0]
    else:
        # Windows can retain older bundles. Ask the WSL shim which driver it
        # actually loads in a short, separate process, without importing torch.
        probe = ('import ctypes, pathlib, sys; '
                 'driver = ctypes.CDLL(sys.argv[1]); '
                 'assert driver.cuInit(0) == 0; '
                 'print(pathlib.Path("/proc/self/maps").read_text())')
        maps = subprocess.check_output([sys.executable, '-c', probe, str(shim)],
                                       env=env, text=True, timeout=15)
        matches = [path for path in candidates if any(
            line.split()[-1].startswith(str(path.parent) + '/libcuda.so')
            for line in maps.splitlines() if line.split())]
        if len(matches) != 1:
            raise RuntimeError('Cannot identify the active WSL NVIDIA driver. '
                               'Set LD_PRELOAD to its matching libnvidia-ptxjitcompiler.so.1.')
        compiler = matches[0]
    previous = env.get('LD_PRELOAD', '')
    env['LD_PRELOAD'] = str(compiler) + (':' + previous if previous else '')
    logger.info('Managed inference uses WSL PTX compiler: %s', compiler)
    return env
