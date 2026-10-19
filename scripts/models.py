"""Preview/remove local model weights, or install ImaJEV, Liquid d1 and Decision3.

Uses only the Python standard library, so cleanup works without uv or a venv.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WEIGHT_PATHS = (
    'models',
    '.inference/upstream/.cache/huggingface',
    '.inference/upstream/adapters',
)


def cleanup_paths(root):
    paths = [root / name for name in WEIGHT_PATHS]
    # Remove stale bundle pointers and hash manifests along with their weights.
    # Check ancestors before globbing, so redirected directories are never read.
    for directory, pattern in (
        (root / '.inference', 'runtime-manifest*.json'),
        (root / '.inference/upstream/artifacts', 'model*.json'),
    ):
        check_path(root, directory / 'placeholder')
        paths.extend(sorted(directory.glob(pattern)))
    for path in paths:
        check_path(root, path)
    return [path for path in paths if path.exists() or path.is_symlink()]


def check_path(root, path):
    for parent in path.relative_to(root).parents:
        if (root / parent).is_symlink():
            raise ValueError(f'Refusing cleanup through a symlink: {root / parent}')


def disk_bytes(path, seen):
    """Count allocated bytes once per inode, without following symbolic links."""
    info = path.lstat()
    identity = (info.st_dev, info.st_ino)
    if identity in seen:
        return 0
    seen.add(identity)
    size = getattr(info, 'st_blocks', None)
    size = info.st_size if size is None else size * 512
    if path.is_dir() and not path.is_symlink():
        size += sum(disk_bytes(child, seen) for child in path.iterdir())
    return size


def clean(root, yes=False):
    paths = cleanup_paths(root)
    seen = set()
    total = 0
    for path in paths:
        size = disk_bytes(path, seen)
        total += size
        print(f'{size / 1024**3:6.2f} GiB  {path.relative_to(root)}')
    print(f'Total model storage: {total / 1024**3:.2f} GiB (allocated, approximate).')
    print('Includes all local experimental models. Keeps runtimes, Python environments, games and rules.')
    print('External Hugging Face caches and Ollama models are outside this cleanup.')
    if not yes:
        print('Preview only. Close the app and inference services, then run: python3 scripts/models.py clean --yes')
        return
    for path in paths:
        check_path(root, path)
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    print('Local model weights removed. Restore ImaJEV, Liquid d1 and Decision3: python3 scripts/models.py install')


def install(root, default_only=False):
    # Keep new downloads in the same local cache that clean manages, even when
    # the caller normally uses an external Hugging Face cache.
    env = os.environ.copy()
    cache = root / '.inference/upstream/.cache/huggingface'
    env.update(HF_HOME=str(cache), HF_HUB_CACHE=str(cache / 'hub'))
    subprocess.run(['bash', str(root / 'scripts/setup_inference.sh')], cwd=root, env=env, check=True)
    if not default_only:
        subprocess.run(['bash', str(root / 'scripts/install_llama_cpp.sh')], cwd=root, env=env, check=True)
        for module, options in (
            ('scripts.install_liquid_d1', ['--with-model']),
            ('scripts.install_decision3', []),
        ):
            subprocess.run(['uv', 'run', '--locked', 'python', '-m', module, *options],
                           cwd=root, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    cleanup = commands.add_parser('clean', help='Preview storage; add --yes to remove all local model weights.')
    cleanup.add_argument('--yes', action='store_true', help='Delete weights; close the app/services first.')
    setup = commands.add_parser('install', help='Install ImaJEV 4B NF4, Liquid d1 Q8, Decision3 Q8 and their runtimes (online).')
    setup.add_argument('--default-only', action='store_true', help='Install only ImaJEV 4B NF4.')
    args = parser.parse_args()
    try:
        if args.command == 'clean':
            clean(ROOT, args.yes)
        else:
            install(ROOT, args.default_only)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
