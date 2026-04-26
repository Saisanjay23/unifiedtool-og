"""
Filesystem helpers for safe writes.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: str | Path, content: str, encoding: str = "utf-8") -> None:
    """
    Atomically replace a text file so readers never observe a partial write.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
        text=True,
    )

    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, destination)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | Path, payload: Any, *, indent: int = 2) -> None:
    """
    Atomically replace a JSON file with a UTF-8 encoded payload.
    """
    atomic_write_text(
        path,
        json.dumps(payload, indent=indent, ensure_ascii=False),
        encoding="utf-8",
    )
