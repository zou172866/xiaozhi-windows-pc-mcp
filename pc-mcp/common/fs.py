"""Atomic file writing utilities for host pc-mcp services (H-5)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Atomically write text via a same-directory temp file + fsync + os.replace."""
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
        os.replace(tmp_path, target)
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
