"""Shared L3/sidecar task ID validation, path containment, and active record pruning (H-5)."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_ACTIVE_TASK_RECORDS = 50
TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
WINDOWS_RESERVED_TASK_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def validate_task_id(task_id: Any, *, task_id_re: re.Pattern[str] = TASK_ID_RE) -> str:
    """Strictly validate a task ID to prevent path traversal or reserved-device writes."""
    if not isinstance(task_id, str):
        raise ValueError("invalid task_id: must be a string")
    if not task_id_re.fullmatch(task_id):
        raise ValueError(f"invalid task_id: {task_id!r}")
    if task_id.upper() in WINDOWS_RESERVED_TASK_NAMES:
        raise ValueError(f"reserved task_id: {task_id!r}")
    return task_id


def resolve_task_file(
    base_dir: Path | str,
    task_id: Any,
    *,
    task_id_re: re.Pattern[str] = TASK_ID_RE,
) -> Path:
    """Resolve `<base_dir>/<task_id>.json` with regex + `.resolve().is_relative_to()` containment."""
    safe_id = validate_task_id(task_id, task_id_re=task_id_re)
    root = Path(base_dir).resolve()
    candidate = (root / f"{safe_id}.json").resolve()
    if not candidate.is_relative_to(root) or candidate.parent != root:
        raise ValueError(f"task path escapes base directory: {task_id!r}")
    return candidate


def task_recency_key(fp: Path, obj: Dict[str, Any], st_mtime_ns: int) -> tuple[float, int, str]:
    started_ts = obj.get("started_ts")
    if started_ts is not None:
        try:
            ts = float(started_ts)
            if ts > 0:
                return (ts, st_mtime_ns, fp.name)
        except (TypeError, ValueError):
            pass
    return (st_mtime_ns / 1_000_000_000.0, st_mtime_ns, fp.name)


def prune_active_tasks(
    active_dir: Path | str,
    *,
    max_keep: int = MAX_ACTIVE_TASK_RECORDS,
    archive_dir: Optional[Path | str] = None,
) -> int:
    """Keep at most `max_keep` recent task records in `active_dir`, archiving older ones cleanly (H-6).

    Currently running or queued task records are never archived prematurely.
    """
    active = Path(active_dir)
    limit = max(1, int(max_keep))
    try:
        if not active.exists():
            return 0
        task_files = [fp for fp in active.glob("*.json") if not fp.is_symlink()]
    except Exception:
        return 0

    if len(task_files) <= limit:
        return 0

    protected: List[Path] = []
    archivable: List[tuple[tuple[float, int, str], Path]] = []
    for fp in task_files:
        try:
            st = fp.stat()
        except OSError:
            continue
        obj: Dict[str, Any] = {}
        try:
            parsed = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                obj = parsed
        except Exception:
            obj = {}
        status = str(obj.get("status") or "").strip().lower()
        if status in ("running", "queued"):
            protected.append(fp)
        else:
            archivable.append((task_recency_key(fp, obj, st.st_mtime_ns), fp))

    keep_other = max(0, limit - len(protected))
    if len(archivable) <= keep_other:
        return 0

    archivable.sort(key=lambda item: item[0], reverse=True)
    to_archive = [fp for _, fp in archivable[keep_other:]]
    if not to_archive:
        return 0

    arch_dir = Path(archive_dir) if archive_dir is not None else (active.parent / "archive")
    try:
        arch_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0

    archived = 0
    for fp in to_archive:
        dst = arch_dir / fp.name
        try:
            os.replace(fp, dst)
            archived += 1
        except OSError:
            try:
                shutil.move(str(fp), str(dst))
                archived += 1
            except Exception:
                continue
    return archived
