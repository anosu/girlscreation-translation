"""Strict JSON IO shared by the translation pipeline."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def digest(value: Any) -> str:
    """Hash portable JSON content, independently of serialization whitespace."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def directory_digest(root: Path, files: list[str] | None = None) -> str:
    """Fingerprint source artifacts with relative paths so snapshots remain portable."""
    values = {}
    paths = root.rglob("*.json") if files is None else (root / name for name in files)
    for path in sorted(paths):
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Source path escapes cache: {path}")
        with path.open("rb") as stream:
            values[path.relative_to(root).as_posix()] = hashlib.file_digest(
                stream, "sha256"
            ).hexdigest()
    return digest(values)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys instead of silently discarding translations."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    """Read UTF-8 JSON and reject duplicate keys."""
    return json.loads(
        Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object
    )


def write_json(path: Path, data: Any) -> None:
    """Atomically replace JSON only when its serialized content changes."""
    write_bytes(
        Path(path),
        (json.dumps(data, ensure_ascii=False, indent=4) + "\n").encode("utf-8"),
    )


def write_bytes(path: Path, content: bytes) -> None:
    """Replace one file atomically, keeping unmodified files untouched."""
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
