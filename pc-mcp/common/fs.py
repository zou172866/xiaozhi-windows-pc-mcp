"""Atomic file writing utilities for host pc-mcp services (H-5)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Atomically write text via a same-directory temp file + fsync + os.replace (with Windows sharing retry)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{target.name}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    tmp_path = target.with_name(tmp_name)
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        max_attempts = 5 if os.name == "nt" else 1
        for attempt in range(max_attempts):
            try:
                os.replace(tmp_path, target)
                break
            except PermissionError:
                if attempt + 1 >= max_attempts:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, payload: Any, indent: Optional[int] = 2) -> None:
    """Atomically serialize JSON to `path`."""
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
