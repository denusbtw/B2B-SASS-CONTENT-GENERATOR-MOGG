import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(os.getenv("MOCK_STORAGE_ROOT", "data/shared"))
MAX_BYTES = 16 * 1024 * 1024


def put(payload: bytes) -> str:
    key = hashlib.sha256(payload).hexdigest()
    ROOT.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=ROOT, delete=False) as output:
        output.write(payload)
        temporary = Path(output.name)
    try:
        temporary.replace(path_for(key))
    finally:
        temporary.unlink(missing_ok=True)
    return key


def path_for(key: str) -> Path:
    if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
        raise ValueError("Invalid SHA-256 locator")
    return ROOT / key


def verified(key: str) -> Path:
    path = path_for(key)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != key:
        raise ValueError("Missing or corrupt artifact")
    return path
