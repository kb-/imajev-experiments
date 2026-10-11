"""Stable local weight identities; hash once per unchanged file in this process."""
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=64)
def _digest(path, size, modified):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return {'path': str(path), 'bytes': stat.st_size,
            'sha256': _digest(str(path), stat.st_size, stat.st_mtime_ns)}
