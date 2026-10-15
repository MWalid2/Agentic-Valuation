"""Source fingerprint so saved outputs can be recognised as stale after code changes."""

import hashlib
from functools import cache
from pathlib import Path


@cache
def build_fingerprint() -> str:
    """Hash of every package source file; independent of Git state and timestamps."""
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]
