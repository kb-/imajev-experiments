"""Install Liquid d1's pinned vision projector, optionally with its Q8 weights."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile

import httpx

REVISION = 'bb1e436ea78eb96a3f1acb6da865f70c2fbeb563'
FILENAME = 'mmproj-d1-3B-Q8_0.gguf'
SHA256 = '2505920ce464b7c54acd8b7b74a931c0c671f06d0c8833760b2fbceeb253e92a'
URL = f'https://huggingface.co/LiquidAI/d1-3B-GGUF/resolve/{REVISION}/{FILENAME}'
MODEL_FILENAME = 'd1-3B-Q8_0.gguf'
MODEL_SHA256 = '2f0942d5a5f64cf69c3356d2be439b71644b1f0eb3a580912a5a0179eeaba77d'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(directory, filename=None, sha256=None, local_name=None):
    filename = filename or FILENAME
    sha256 = sha256 or SHA256
    label = 'projector' if filename == FILENAME else 'model'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (local_name or filename)
    if destination.exists():
        if digest(destination) != sha256:
            raise ValueError(f'Existing {label} checksum differs: {destination}. Move it aside before installing.')
        print(f'Asset already verified: {destination}', flush=True)
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=filename + '.', suffix='.part', delete=False) as output:
            temporary = Path(output.name)
            received = 0
            checksum = hashlib.sha256()
            print(f'Downloading {filename} from revision {REVISION}…', flush=True)
            url = f'https://huggingface.co/LiquidAI/d1-3B-GGUF/resolve/{REVISION}/{filename}'
            with httpx.stream('GET', url, follow_redirects=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 1024):
                    output.write(chunk)
                    checksum.update(chunk)
                    received += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if checksum.hexdigest() != sha256:
            raise ValueError('Downloaded asset checksum differs from the publisher asset.')
        os.replace(temporary, destination)
        print(f'Installed and verified {received / 1_000_000:.1f} MB: {destination}', flush=True)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-directory', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'models/liquid-d1-3b')
    parser.add_argument('--with-model', action='store_true', help='Also install pinned Q8_0 weights as model.gguf.')
    args = parser.parse_args()
    if args.with_model:
        install(args.model_directory, MODEL_FILENAME, MODEL_SHA256, 'model.gguf')
    install(args.model_directory)
    if not (args.model_directory / 'model.gguf').is_file():
        print('Place the matching d1-3B Q8_0 weights at model.gguf before launching.')


if __name__ == '__main__':
    main()
