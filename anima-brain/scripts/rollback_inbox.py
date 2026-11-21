#!/usr/bin/env python3
"""Safe rollback utility for B-1 persistent xiaozhi_inbox table in brain.sqlite3.

Usage (stop xiaozhi-anima-brain container first before modifying live brain.sqlite3):
  python anima-brain/scripts/rollback_inbox.py --db anima-data/brain.sqlite3 --dry-run
  python anima-brain/scripts/rollback_inbox.py --db anima-data/brain.sqlite3
  python anima-brain/scripts/rollback_inbox.py --db anima-data/brain.sqlite3 --drain --export /tmp/inbox_backup.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"pending": 0, "processing": 0, "done": 0, "dead": 0}
    for r in conn.execute("SELECT status, count(*) AS cnt FROM xiaozhi_inbox GROUP BY status").fetchall():
        st = str(r["status"] or "")
        counts[st] = int(r["cnt"] or 0)
    return counts


def _serialize_row(r: sqlite3.Row) -> dict[str, Any]:
    raw_payload = str(r["payload"] or "")
    try:
        parsed_payload: Any = json.loads(raw_payload)
    except Exception:
        parsed_payload = None
    return {
        "request_id": str(r["request_id"]),
        "character_id": str(r["character_id"] or ""),
        "status": str(r["status"] or ""),
        "attempts": int(r["attempts"] or 0),
        "last_error": str(r["last_error"] or ""),
        "created_at": float(r["created_at"] or 0.0),
        "updated_at": float(r["updated_at"] or 0.0),
        "payload_raw": raw_payload,
        "payload": parsed_payload,
    }


def rollback_inbox(
    db_path: Path | str,
    *,
    export_path: Path | str | None = None,
    dry_run: bool = False,
    drop_table: bool = True,
    include_done: bool = False,
    drain_fn: Callable[[dict[str, Any]], Any] | None = None,
    force_drop_on_drain_error: bool = False,
) -> dict[str, Any]:
    """Safely drain and/or export uncompleted xiaozhi_inbox rows, then drop xiaozhi_inbox cleanly.

    Idempotent: if xiaozhi_inbox does not exist, succeeds cleanly without mutating the database.
    """
    db_file = Path(db_path)
    if not db_file.is_file():
        raise FileNotFoundError(f"SQLite database not found: {db_file}")

    conn = sqlite3.connect(str(db_file), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "xiaozhi_inbox"):
            return {
                "ok": True,
                "table_existed": False,
                "counts": {},
                "drained": 0,
                "drain_failed": 0,
                "exported": 0,
                "export_path": None,
                "dropped": False,
                "dry_run": bool(dry_run),
                "integrity_ok": True,
                "wal_checkpoint_ok": True,
            }

        counts_initial = _count_by_status(conn)
        if dry_run:
            export_candidates = (
                sum(counts_initial.values())
                if include_done
                else sum(v for k, v in counts_initial.items() if k != "done")
            )
            return {
                "ok": True,
                "table_existed": True,
                "counts": counts_initial,
                "drained": 0,
                "drain_failed": 0,
                "exported": export_candidates,
                "export_path": str(export_path) if export_path is not None else None,
                "dropped": False,
                "dry_run": True,
                "integrity_ok": True,
                "wal_checkpoint_ok": True,
            }

        drained = 0
        drain_failed = 0
        if drain_fn is not None:
            pending_rows = conn.execute(
                "SELECT request_id, character_id, payload, status, attempts, last_error, created_at, updated_at "
                "FROM xiaozhi_inbox WHERE status IN ('pending', 'processing') "
                "ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
            for r in pending_rows:
                row_dict = _serialize_row(r)
                try:
                    res = drain_fn(row_dict)
                    # P1-8: Conflict results (committed=True, conflict=True) MUST NOT be treated as drained/done.
                    if isinstance(res, dict):
                        succeeded = (
                            (bool(res.get("committed")) and not bool(res.get("conflict")))
                            or res.get("skipped") == "empty_message"
                        )
                    else:
                        succeeded = bool(res)
                except Exception:
                    succeeded = False
                if succeeded:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.execute(
                            "UPDATE xiaozhi_inbox SET status='done', last_error='', updated_at=? "
                            "WHERE request_id=?",
                            (time.time(), row_dict["request_id"]),
                        )
                        conn.execute("COMMIT")
                    except Exception:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                        raise
                    drained += 1
                else:
                    drain_failed += 1
            # Release any external brain handle held by drain_fn before acquiring EXCLUSIVE lock / WAL checkpoint
            closer = getattr(drain_fn, "_close_brain", None)
            if callable(closer):
                closer()

        # P1-8: Export and DROP TABLE inside a SINGLE BEGIN EXCLUSIVE transaction so no concurrent
        # writer can sneak rows in between export snapshot and DROP TABLE (eliminates TOCTOU row loss).
        written_export_path: str | None = None
        dropped = False
        counts_before_drop: dict[str, int] = {}
        serialized_rows: list[dict[str, Any]] = []

        conn.execute("BEGIN EXCLUSIVE")
        try:
            if not _table_exists(conn, "xiaozhi_inbox"):
                conn.execute("ROLLBACK")
                return {
                    "ok": True,
                    "table_existed": False,
                    "counts": {},
                    "drained": drained,
                    "drain_failed": drain_failed,
                    "exported": 0,
                    "export_path": None,
                    "dropped": False,
                    "dry_run": False,
                    "integrity_ok": True,
                    "wal_checkpoint_ok": True,
                }

            counts_before_drop = _count_by_status(conn)
            if include_done:
                rows_to_export = conn.execute(
                    "SELECT request_id, character_id, payload, status, attempts, last_error, created_at, updated_at "
                    "FROM xiaozhi_inbox ORDER BY created_at ASC, rowid ASC"
                ).fetchall()
            else:
                rows_to_export = conn.execute(
                    "SELECT request_id, character_id, payload, status, attempts, last_error, created_at, updated_at "
                    "FROM xiaozhi_inbox WHERE status != 'done' ORDER BY created_at ASC, rowid ASC"
                ).fetchall()

            serialized_rows = [_serialize_row(r) for r in rows_to_export]
            if serialized_rows or export_path is not None:
                if export_path is not None:
                    out_path = Path(export_path)
                else:
                    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    out_path = db_file.parent / f"xiaozhi_inbox_rollback_{stamp}.json"
                export_doc = {
                    "schema_version": 1,
                    "exported_at": datetime.datetime.now().astimezone().isoformat(),
                    "db_path": str(db_file.resolve()),
                    "counts_before_drop": counts_before_drop,
                    "exported_count": len(serialized_rows),
                    "rows": serialized_rows,
                }
                _atomic_write_json(out_path, export_doc)
                # Verify exported archive parses cleanly with exact row count before any table drop
                verified = json.loads(out_path.read_text(encoding="utf-8"))
                if not isinstance(verified, dict) or len(verified.get("rows") or []) != len(serialized_rows):
                    raise RuntimeError(f"Export verification failed for {out_path}")
                written_export_path = str(out_path)

            if drain_failed > 0 and not force_drop_on_drain_error:
                conn.execute("ROLLBACK")
                return {
                    "ok": False,
                    "error": f"drain_incomplete ({drain_failed} row(s) failed to drain; table preserved)",
                    "table_existed": True,
                    "counts": counts_before_drop,
                    "drained": drained,
                    "drain_failed": drain_failed,
                    "exported": len(serialized_rows),
                    "export_path": written_export_path,
                    "dropped": False,
                    "dry_run": False,
                    "integrity_ok": True,
                    "wal_checkpoint_ok": True,
                }

            if drop_table:
                conn.execute("DROP INDEX IF EXISTS idx_xiaozhi_inbox_status")
                conn.execute("DROP TABLE IF EXISTS xiaozhi_inbox")
                conn.execute("COMMIT")
                dropped = True
            else:
                conn.execute("ROLLBACK")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        wal_checkpoint_ok = True
        wal_checkpoint_info: dict[str, int] | None = None
        wal_error = ""
        if dropped:
            try:
                cp_row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if cp_row is not None:
                    busy = int(cp_row[0])
                    log_frames = int(cp_row[1])
                    checkpointed = int(cp_row[2])
                    wal_checkpoint_info = {
                        "busy": busy,
                        "log": log_frames,
                        "checkpointed": checkpointed,
                    }
                    if busy != 0:
                        wal_checkpoint_ok = False
                        wal_error = (
                            f"wal_checkpoint_busy (busy={busy}, log={log_frames}, checkpointed={checkpointed}; "
                            "another reader/process holds the WAL open)"
                        )
            except sqlite3.OperationalError as cp_exc:
                wal_checkpoint_ok = False
                wal_error = f"wal_checkpoint_error: {cp_exc}"

        chk_rows = conn.execute("PRAGMA integrity_check").fetchall()
        integrity_ok = len(chk_rows) == 1 and str(chk_rows[0][0]).strip().lower() == "ok"

        ok = bool(integrity_ok) and bool(wal_checkpoint_ok)
        result: dict[str, Any] = {
            "ok": ok,
            "table_existed": True,
            "counts": counts_before_drop,
            "drained": drained,
            "drain_failed": drain_failed,
            "exported": len(serialized_rows),
            "export_path": written_export_path,
            "dropped": dropped,
            "dry_run": False,
            "integrity_ok": integrity_ok,
            "wal_checkpoint_ok": wal_checkpoint_ok,
        }
        if wal_checkpoint_info is not None:
            result["wal_checkpoint"] = wal_checkpoint_info
        if wal_error:
            result["error"] = wal_error
        return result
    finally:
        conn.close()


def _build_default_drain_fn(db_path: Path):
    """Build a CompanionBrain-backed drain callback for --drain CLI mode."""
    anima_root = Path(__file__).resolve().parent.parent
    src_dir = anima_root / "src"
    for p in (str(anima_root), str(src_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)

    from companion_engine import BrainConfig, CompanionBrain, Settings
    import xiaozhi_anima_service as svc

    brain = CompanionBrain(
        db_path,
        user_id="local",
        settings=Settings.from_env(),
        config=BrainConfig(start_worker=False),
    )
    svc.ensure_default_character(brain)
    closed_flag = {"done": False}

    def _close_brain() -> None:
        if not closed_flag["done"]:
            closed_flag["done"] = True
            brain.close()

    def _drain(row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid payload JSON")
        u_msg = str(payload.get("user_message") or "").strip()
        a_msg = str(payload.get("assistant_reply") or "").strip()
        meta = payload.get("turn_meta") if isinstance(payload.get("turn_meta"), dict) else {}
        return svc.extract_and_commit_turn(
            brain,
            str(row.get("character_id") or svc.DEFAULT_CHARACTER_ID),
            str(row.get("request_id") or ""),
            u_msg,
            a_msg,
            turn_meta=meta or None,
        )

    _drain._close_brain = _close_brain  # type: ignore[attr-defined]
    return brain, _drain


def main(argv: list[str] | None = None) -> int:
    default_db = os.environ.get("ANIMA_DB_PATH") or str(
        Path(os.environ.get("ANIMA_DATA_DIR", "/data/anima")) / "brain.sqlite3"
    )
    parser = argparse.ArgumentParser(
        description="Safely drain/export pending xiaozhi_inbox rows and drop xiaozhi_inbox table."
    )
    parser.add_argument("--db", default=default_db, help="Path to brain.sqlite3")
    parser.add_argument("--export", default=None, help="Output JSON path for uncompleted inbox rows")
    parser.add_argument("--dry-run", action="store_true", help="Inspect counts without modifying DB")
    parser.add_argument("--no-drop", action="store_true", help="Export/drain only; keep xiaozhi_inbox table")
    parser.add_argument("--include-done", action="store_true", help="Also include status='done' rows in export")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Attempt to commit pending/processing rows into turns before exporting/dropping",
    )
    parser.add_argument(
        "--force-drop",
        action="store_true",
        help="Drop xiaozhi_inbox even if --drain had failed rows (still exports failed rows first)",
    )
    args = parser.parse_args(argv)

    brain = None
    drain_fn = None
    try:
        if args.drain and not args.dry_run:
            brain, drain_fn = _build_default_drain_fn(Path(args.db))
        result = rollback_inbox(
            args.db,
            export_path=args.export,
            dry_run=args.dry_run,
            drop_table=not args.no_drop,
            include_done=args.include_done,
            drain_fn=drain_fn,
            force_drop_on_drain_error=args.force_drop,
        )
    finally:
        if brain is not None:
            try:
                closer = getattr(drain_fn, "_close_brain", None)
                if callable(closer):
                    closer()
                else:
                    brain.close()
            except Exception:
                pass

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
