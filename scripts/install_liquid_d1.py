"""Install Liquid d1's pinned vision projector alongside existing Q8 weights."""
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


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / FILENAME
    if destination.exists():
        if digest(destination) != SHA256:
            raise ValueError(f'Existing projector checksum differs: {destination}. Move it aside before installing.')
        print(f'Projector already verified: {destination}', flush=True)
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=FILENAME + '.', suffix='.part', delete=False) as output:
            temporary = Path(output.name)
            received = 0
            checksum = hashlib.sha256()
            print(f'Downloading {FILENAME} from revision {REVISION}…', flush=True)
            with httpx.stream('GET', URL, follow_redirects=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 1024):
                    output.write(chunk)
                    checksum.update(chunk)
                    received += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if checksum.hexdigest() != SHA256:
            raise ValueError('Downloaded projector checksum differs from the publisher asset.')
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
    args = parser.parse_args()
    install(args.model_directory)
    if not (args.model_directory / 'model.gguf').is_file():
        print('Place the matching d1-3B Q8_0 weights at model.gguf before launching.')


if __name__ == '__main__':
    main()
