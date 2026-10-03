"""Validate pinned local assets before starting the upstream service. No downloads."""
import argparse
import hashlib
from importlib.metadata import version as package_version
import json
from pathlib import Path
import subprocess

UPSTREAM_COMMIT = 'ccf586d43d2a580319b6535c893668904d909eb9'
BITSANDBYTES_VERSION = '0.50.2'

SPECS = {
    '2b': {
        'bundle': 'artifacts/model.json',
        'base_revision': '15852e8c16360a2fea060d615a32b45270f8a8fc',
        'adapter': 'adapters/imajev-2b',
        'adapter_revision': '0426f7b1c73804b64fab5802e04f401420ec774c',
        'adapter_files': (
            'adapter_config.json',
            'adapter_model.safetensors',
            'decision_readout.json',
            'decision_readout.safetensors',
            'calibration.json',
        ),
        'manifest': 'runtime-manifest.json',
    },
    '4b-nf4': {
        'bundle': 'artifacts/model-qwen4b.json',
        'base_revision': '851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a',
        'adapter': 'adapters/imajev-4b',
        'adapter_revision': 'f8d8234cebc6c99065c07731e59716dc0a6e27ab',
        'adapter_files': (
            'adapter_config.json',
            'adapter_model.safetensors',
            'decision_readout.json',
            'decision_readout.safetensors',
            'calibration-rot4.json',
        ),
        'manifest': 'runtime-manifest-4b-nf4.json',
        'quantization': {
            'backend': 'bitsandbytes',
            'version': BITSANDBYTES_VERSION,
            'format': 'nf4',
            'compute_dtype': 'bfloat16',
            'double_quant': True,
        },
    },
}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--model', choices=tuple(SPECS), default='4b-nf4')
    parser.add_argument('--record', action='store_true', help='Record asset hashes after online setup')
    args = parser.parse_args()
    spec = SPECS[args.model]
    root = args.upstream.resolve()

    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != UPSTREAM_COMMIT:
        raise SystemExit('Unexpected upstream revision. Run the matching setup_inference script.')
    if subprocess.check_output(
        ['git', '-C', str(root), 'diff', '--name-only', 'HEAD', '--', 'src', 'scripts', 'pyproject.toml'],
        text=True,
    ).strip():
        raise SystemExit('Upstream tracked files were modified. Restore the pinned checkout.')

    bundle = json.loads((root / spec['bundle']).read_text())
    if bundle['revision'] != spec['base_revision']:
        raise SystemExit('Unexpected base model revision.')
    base = Path(bundle['path'])
    if not base.is_absolute():
        base = root / base
    adapter = root / spec['adapter']

    required = ['config.json', 'tokenizer_config.json', 'tokenizer.json', 'preprocessor_config.json']
    files = [base / name for name in required]
    index = base / 'model.safetensors.index.json'
    if index.exists():
        files.append(index)
        files.extend(base / name for name in sorted(set(json.loads(index.read_text())['weight_map'].values())))
    else:
        files.append(base / 'model.safetensors')
    files.extend(adapter / name for name in spec['adapter_files'])

    for path in files:
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f'Missing model asset: {path}. Run the matching setup_inference script while online.')

    if args.model == '4b-nf4':
        installed = package_version('bitsandbytes')
        if installed != BITSANDBYTES_VERSION:
            raise SystemExit(
                f'bitsandbytes {BITSANDBYTES_VERSION} is required for 4b-nf4; found {installed}. '
                'Run scripts/setup_inference_4b_nf4.sh while online.'
            )

    manifest_path = root.parent / spec['manifest']
    assets = {
        str(path): digest(path)
        for directory in (base, adapter)
        for path in sorted(directory.rglob('*'))
        if path.is_file() and '.cache' not in path.relative_to(directory).parts
    }
    manifest = {
        'version': 1,
        'upstream_commit': commit,
        'base_revision': spec['base_revision'],
        'adapter_revision': spec['adapter_revision'],
        'assets_sha256': assets,
    }
    if args.model == '4b-nf4':
        manifest = {
            'version': 2,
            'model': args.model,
            'upstream_commit': commit,
            'base_revision': spec['base_revision'],
            'adapter_revision': spec['adapter_revision'],
            'quantization': spec['quantization'],
            'assets_sha256': assets,
        }

    if args.record:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print(f'Recorded offline asset manifest: {manifest_path}')
    else:
        if not manifest_path.exists() or json.loads(manifest_path.read_text()) != manifest:
            raise SystemExit('Assets differ from the setup manifest. Restore or prepare the pinned assets.')
        import torch
        if not torch.cuda.is_available():
            raise SystemExit('CUDA is unavailable. Fix the NVIDIA/WSL setup; no CPU fallback is enabled.')
        print(f'Assets verified for {args.model}. GPU: {torch.cuda.get_device_name(0)}')


if __name__ == '__main__':
    main()
