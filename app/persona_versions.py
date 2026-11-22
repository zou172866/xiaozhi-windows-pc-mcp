"""Persona prompt version snapshots with CAS, digest verification, diff, one-click rollback, and policy boundary checks.

Persists up to MAX_SNAPSHOTS recent persona revisions in `state/persona_versions.json`.
Enforces the strict boundary invariant:
  Persona prompts define conversational personality and tone ONLY; they NEVER grant tool
  permissions and NEVER override host `pc_control_mode.json` policy or safety guardrails.
"""

from __future__ import annotations

from datetime import datetime
import difflib
import hashlib
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import xz_common

SCHEMA_VERSION = 1
MAX_SNAPSHOTS = 20
POLICY_BOUNDARY_CODE = "persona_prompt_never_grants_tool_permissions_or_overrides_host_pc_mode"
POLICY_BOUNDARY_NOTE_ZH = "人设仅定义性格与口吻，不授予工具权限，也不替代宿主机 pc_control_mode 挡位策略"


def _resolve_default_path() -> str:
    return os.path.join(xz_common.STATE_DIR, "persona_versions.json")


PERSONA_VERSIONS_PATH = _resolve_default_path()
_lock = threading.RLock()


class PersonaConflictError(ValueError):
    """Raised when CAS (`expected_revision` / `expected_digest`) fails on persona save or rollback."""

    def __init__(
        self,
        message: str,
        *,
        current_revision: int = 0,
        current_digest: str = "",
    ):
        super().__init__(message)
        self.current_revision = int(current_revision)
        self.current_digest = str(current_digest)


class PersonaBoundaryError(ValueError):
    """Raised when a persona prompt attempts to grant tool permissions or override host policy."""

    def __init__(self, message: str, *, violations: Optional[List[str]] = None):
        super().__init__(message)
        self.violations = list(violations or [])


_BOUNDARY_VIOLATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "host_policy_file_override",
        re.compile(
            r"(?i)(?:pc_control_mode\.json|pc_mode\s*[:=]\s*[\"']?full[\"']?|XIAOZHI_ALLOW_INSECURE_BOOT\s*=\s*1|FAILSAFE\s*[:=]\s*False)"
        ),
    ),
    (
        "host_tier_bypass_zh",
        re.compile(
            r"(?:绕过|无视|忽略|覆盖|跳过|突破).{0,8}(?:宿主机挡位|权限挡位|安全挡|权限校验|安全护栏|L2/?L3拦截|L3拦截)"
        ),
    ),
    (
        "force_unlock_full_tier_zh",
        re.compile(r"(?:强制解锁|自动切换至|直接授予).{0,8}(?:全权操控挡|全权挡|最高工具权限|全部系统权限|管理员权限)"),
    ),
    (
        "policy_override_en",
        re.compile(
            r"(?i)(?:override|bypass|ignore|disable)\s+(?:the\s+)?(?:host\s+)?(?:pc_mode|pc_control_mode|tool\s+policy|tier\s+policy|security\s+policy|safety\s+guardrails?|failsafe)"
        ),
    ),
    (
        "grant_privilege_en",
        re.compile(
            r"(?i)(?:grant|elevate)\s+(?:yourself|this\s+persona|the\s+assistant)\s+(?:full|root|admin|unrestricted)\s+(?:tool|computer|host)\s+permissions?"
        ),
    ),
)


def compute_prompt_digest(prompt: str) -> str:
    """Compute canonical SHA-256 digest of a normalized persona prompt."""
    clean = str(prompt or "").strip().replace("\r\n", "\n")
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def validate_persona_boundaries(prompt: str) -> None:
    """Reject explicit tool-permission-grant or host-policy-override directives inside persona prompts."""
    text = str(prompt or "")
    hits: List[str] = []
    for code, pat in _BOUNDARY_VIOLATION_PATTERNS:
        if pat.search(text):
            hits.append(code)
    if hits:
        raise PersonaBoundaryError(
            f"人设边界校验未通过（{POLICY_BOUNDARY_NOTE_ZH}），检测到越权/绕过策略指令: {', '.join(hits)}",
            violations=hits,
        )


def _now_iso(ts: float | None = None) -> str:
    t = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _default_store() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "current_revision": 0,
        "current_digest": "",
        "policy_boundary": POLICY_BOUNDARY_CODE,
        "anima_sync": {
            "ok": None,
            "revision": 0,
            "digest": "",
            "char_name": "",
            "synced_at": "",
            "detail": "",
        },
        "snapshots": [],
    }


def _normalize_store(raw: Any) -> Dict[str, Any]:
    base = _default_store()
    if not isinstance(raw, dict):
        return base
    snaps: List[Dict[str, Any]] = []
    for item in raw.get("snapshots") or []:
        if not isinstance(item, dict):
            continue
        p = str(item.get("prompt") or "").strip()
        if not p:
            continue
        rev = int(item.get("revision") or 0)
        if rev <= 0:
            continue
        dig = str(item.get("digest") or "").strip() or compute_prompt_digest(p)
        snaps.append(
            {
                "revision": rev,
                "digest": dig,
                "prompt": p,
                "char_name": str(item.get("char_name") or "").strip()[:32],
                "created_at": str(item.get("created_at") or ""),
                "created_ts": float(item.get("created_ts") or 0.0),
                "source": str(item.get("source") or "console_save")[:32],
                "note": str(item.get("note") or "")[:160],
                "rolled_back_from": (
                    int(item["rolled_back_from"])
                    if item.get("rolled_back_from") is not None
                    else None
                ),
            }
        )
    snaps.sort(key=lambda s: s["revision"])
    snaps = snaps[-MAX_SNAPSHOTS:]
    cur_rev = int(raw.get("current_revision") or (snaps[-1]["revision"] if snaps else 0))
    cur_dig = str(raw.get("current_digest") or (snaps[-1]["digest"] if snaps else ""))
    if snaps and snaps[-1]["revision"] > cur_rev:
        cur_rev = snaps[-1]["revision"]
        cur_dig = snaps[-1]["digest"]

    sync_raw = raw.get("anima_sync") if isinstance(raw.get("anima_sync"), dict) else {}
    base["current_revision"] = cur_rev
    base["current_digest"] = cur_dig
    base["snapshots"] = snaps
    base["anima_sync"] = {
        "ok": sync_raw.get("ok") if isinstance(sync_raw.get("ok"), bool) else None,
        "revision": int(sync_raw.get("revision") or 0),
        "digest": str(sync_raw.get("digest") or ""),
        "char_name": str(sync_raw.get("char_name") or ""),
        "synced_at": str(sync_raw.get("synced_at") or ""),
        "detail": str(sync_raw.get("detail") or "")[:160],
    }
    return base


def _read_store_locked(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return _default_store()
    try:
        import json

        with open(path, "r", encoding="utf-8") as f:
            return _normalize_store(json.load(f))
    except Exception:
        return _default_store()


def _write_store_locked(path: str, data: Dict[str, Any]) -> None:
    xz_common.atomic_write_json(path, data)


def ensure_synced_with_prompt(
    current_prompt: str,
    *,
    char_name: str = "",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure revision store is initialized and synced with live `.config.yaml` prompt."""
    target = os.path.abspath(path or PERSONA_VERSIONS_PATH)
    clean_prompt = str(current_prompt or "").strip().replace("\r\n", "\n")
    with _lock:
        store = _read_store_locked(target)
        if not clean_prompt:
            return store
        cur_dig = compute_prompt_digest(clean_prompt)
        if store["current_revision"] == 0 or store["current_digest"] != cur_dig:
            next_rev = int(store["current_revision"]) + 1
            now_ts = time.time()
            snap = {
                "revision": next_rev,
                "digest": cur_dig,
                "prompt": clean_prompt,
                "char_name": str(char_name or "").strip()[:32],
                "created_at": _now_iso(now_ts),
                "created_ts": now_ts,
                "source": "initial" if store["current_revision"] == 0 else "external_sync",
                "note": "初始人设基线" if store["current_revision"] == 0 else "检测到外部 YAML 变更并自动建档",
                "rolled_back_from": None,
            }
            store["snapshots"] = (store["snapshots"] + [snap])[-MAX_SNAPSHOTS:]
            store["current_revision"] = next_rev
            store["current_digest"] = cur_dig
            try:
                _write_store_locked(target, store)
            except Exception:
                pass
        return store


def verify_cas(
    current_prompt: str,
    *,
    expected_revision: Optional[int] = None,
    expected_digest: Optional[str] = None,
    char_name: str = "",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify optional CAS preconditions before committing a persona mutation."""
    target = os.path.abspath(path or PERSONA_VERSIONS_PATH)
    with _lock:
        store = ensure_synced_with_prompt(current_prompt, char_name=char_name, path=target)
        cur_rev = int(store["current_revision"])
        cur_dig = str(store["current_digest"])
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
                raise ValueError("字段 expected_prompt_revision 必须为非负整数")
            if expected_revision != cur_rev:
                raise PersonaConflictError(
                    f"人设版本冲突（CAS 校验失败）：期望版本 rev.{expected_revision}，当前最新版本为 rev.{cur_rev}，请刷新后重试",
                    current_revision=cur_rev,
                    current_digest=cur_dig,
                )
        if expected_digest is not None:
            exp_d = str(expected_digest).strip().lower()
            if not exp_d:
                raise ValueError("字段 expected_prompt_digest 不能为空")
            if cur_dig and exp_d != cur_dig.lower():
                raise PersonaConflictError(
                    f"人设摘要冲突（CAS 校验失败）：期望摘要 {exp_d[:12]}，当前摘要为 {cur_dig[:12]}，请刷新后重试",
                    current_revision=cur_rev,
                    current_digest=cur_dig,
                )
        return store


def record_persona_version(
    prompt: str,
    *,
    char_name: str = "",
    expected_revision: Optional[int] = None,
    expected_digest: Optional[str] = None,
    current_prompt_on_disk: Optional[str] = None,
    source: str = "console_save",
    note: str = "",
    rolled_back_from: Optional[int] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate boundaries + CAS and atomically append a new persona revision snapshot."""
    clean_prompt = str(prompt or "").strip().replace("\r\n", "\n")
    if not clean_prompt:
        raise ValueError("角色提示词不能为空")
    validate_persona_boundaries(clean_prompt)

    target = os.path.abspath(path or PERSONA_VERSIONS_PATH)
    with _lock:
        if current_prompt_on_disk is not None:
            store = verify_cas(
                current_prompt_on_disk,
                expected_revision=expected_revision,
                expected_digest=expected_digest,
                char_name=char_name,
                path=target,
            )
        else:
            store = _read_store_locked(target)
            if expected_revision is not None:
                if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
                    raise ValueError("字段 expected_prompt_revision 必须为非负整数")
                if expected_revision != int(store["current_revision"]):
                    raise PersonaConflictError(
                        f"人设版本冲突：期望 rev.{expected_revision}，当前 rev.{store['current_revision']}",
                        current_revision=store["current_revision"],
                        current_digest=store["current_digest"],
                    )
            if expected_digest is not None:
                exp_d = str(expected_digest).strip().lower()
                if not exp_d:
                    raise ValueError("字段 expected_prompt_digest 不能为空")
                if store["current_digest"] and exp_d != str(store["current_digest"]).lower():
                    raise PersonaConflictError(
                        "人设摘要冲突，请刷新后重试",
                        current_revision=store["current_revision"],
                        current_digest=store["current_digest"],
                    )

        new_dig = compute_prompt_digest(clean_prompt)
        if new_dig == store["current_digest"] and rolled_back_from is None and store["current_revision"] > 0:
            return {
                "revision": store["current_revision"],
                "digest": store["current_digest"],
                "changed": False,
                "snapshot_count": len(store["snapshots"]),
            }

        next_rev = int(store["current_revision"]) + 1
        now_ts = time.time()
        snap = {
            "revision": next_rev,
            "digest": new_dig,
            "prompt": clean_prompt,
            "char_name": str(char_name or "").strip()[:32],
            "created_at": _now_iso(now_ts),
            "created_ts": now_ts,
            "source": str(source or "console_save")[:32],
            "note": str(note or "").strip()[:160],
            "rolled_back_from": int(rolled_back_from) if rolled_back_from is not None else None,
        }
        store["snapshots"] = (store["snapshots"] + [snap])[-MAX_SNAPSHOTS:]
        store["current_revision"] = next_rev
        store["current_digest"] = new_dig
        _write_store_locked(target, store)
        return {
            "revision": next_rev,
            "digest": new_dig,
            "changed": True,
            "snapshot": dict(snap),
            "snapshot_count": len(store["snapshots"]),
        }


def get_snapshot(
    revision: int,
    *,
    current_prompt: Optional[str] = None,
    char_name: str = "",
    path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    target = os.path.abspath(path or PERSONA_VERSIONS_PATH)
    with _lock:
        store = (
            ensure_synced_with_prompt(current_prompt, char_name=char_name, path=target)
            if current_prompt is not None
            else _read_store_locked(target)
        )
        for snap in store["snapshots"]:
            if int(snap["revision"]) == int(revision):
                return dict(snap)
    return None


def list_persona_history(
    *,
    current_prompt: Optional[str] = None,
    char_name: str = "",
    include_full_prompt: bool = False,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    target = os.path.abspath(path or PERSONA_VERSIONS_PATH)
    with _lock:
        store = (
            ensure_synced_with_prompt(current_prompt, char_name=char_name, path=target)
            if current_prompt is not None
            else _read_store_locked(target)
        )
        items = []
        for s in reversed(store["snapshots"]):
            p = str(s.get("prompt") or "")
            entry = {
                "revision": int(s["revision"]),
                "digest": str(s["digest"]),
                "digest_short": str(s["digest"])[:12],
                "char_name": str(s.get("char_name") or ""),
                "created_at": str(s.get("created_at") or ""),
                "created_ts": float(s.get("created_ts") or 0.0),
                "source": str(s.get("source") or ""),
                "note": str(s.get("note") or ""),
                "rolled_back_from": s.get("rolled_back_from"),
                "char_count": len(p),
                "preview": p[:120] + ("…" if len(p) > 120 else ""),
                "is_current": int(s["revision"]) == int(store["current_revision"]),
            }
            if include_full_prompt:
                entry["prompt"] = p
            items.append(entry)
        return {
            "current_revision": int(store["current_revision"]),
            "current_digest": str(store["current_digest"]),
            "current_digest_short": str(store["current_digest"])[:12],
            "max_snapshots": MAX_SNAPSHOTS,
            "snapshot_count": len(items),
            "policy_boundary": POLICY_BOUNDARY_CODE,
            "boundary_note": POLICY_BOUNDARY_NOTE_ZH,
            "anima_sync": dict(store.get("anima_sync") or {}),
            "snapshots": items,
        }


def diff_persona_versions(
    from_revision: int,
    to_revision: Optional[int] = None,
    *,
    current_prompt: Optional[str] = None,
    char_name: str = "",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    target = os.path.abspath(path or PERSONA_VERSIONS_PATH)
    with _lock:
        store = (
            ensure_synced_with_prompt(current_prompt, char_name=char_name, path=target)
            if current_prompt is not None
            else _read_store_locked(target)
        )
        by_rev = {int(s["revision"]): s for s in store["snapshots"]}
        if int(from_revision) not in by_rev:
            raise KeyError(f"未找到人设快照版本 rev.{from_revision}")
        eff_to = int(store["current_revision"]) if to_revision is None else int(to_revision)
        if eff_to not in by_rev:
            raise KeyError(f"未找到人设快照版本 rev.{eff_to}")
        old_s = by_rev[int(from_revision)]
        new_s = by_rev[eff_to]
        old_lines = str(old_s["prompt"]).splitlines()
        new_lines = str(new_s["prompt"]).splitlines()
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"persona@rev.{old_s['revision']} ({old_s['digest'][:8]})",
                tofile=f"persona@rev.{new_s['revision']} ({new_s['digest'][:8]})",
                lineterm="",
                n=2,
            )
        )
        added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
        return {
            "from_revision": int(old_s["revision"]),
            "to_revision": int(new_s["revision"]),
            "from_digest": str(old_s["digest"]),
            "to_digest": str(new_s["digest"]),
            "identical": old_s["digest"] == new_s["digest"],
            "added_lines": added,
            "removed_lines": removed,
            "unified_diff": "\n".join(diff_lines) if diff_lines else "(两版本正文完全一致)",
        }


def record_anima_sync_status(
    *,
    ok: bool,
    revision: Optional[int] = None,
    digest: str = "",
    char_name: str = "",
    detail: str = "",
    path: Optional[str] = None,
) -> None:
    target = os.path.abspath(path or PERSONA_VERSIONS_PATH)
    with _lock:
        store = _read_store_locked(target)
        store["anima_sync"] = {
            "ok": bool(ok),
            "revision": int(revision if revision is not None else store["current_revision"]),
            "digest": str(digest or store["current_digest"]),
            "char_name": str(char_name or "")[:32],
            "synced_at": _now_iso(),
            "detail": str(detail or "")[:160],
        }
        try:
            _write_store_locked(target, store)
        except Exception:
            pass
