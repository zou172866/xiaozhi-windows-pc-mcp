"""Startup patch registry, upstream contract verification, and module-wide symbol rebinding.

Provides:
- P1-8: Upstream SERVER_VERSION + inspect.signature contract checks, patch status tracking
  (ok / skipped / failed), and /api/health degradation reporting.
- P1-12: Universal rebind(old_obj, new_obj) across sys.modules so early-imported module bindings
  (e.g. listenMessageHandler.send_tts_message) never bypass patched functions.
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_EXPECTED_SERVER_VERSION = "0.9.8"
VALID_PATCH_STATUSES = frozenset({"ok", "skipped", "failed"})

_lock = threading.RLock()
_patches: Dict[str, Dict[str, Any]] = {}
_version_info: Dict[str, Any] = {
    "checked": False,
    "expected": DEFAULT_EXPECTED_SERVER_VERSION,
    "actual": "",
    "match": True,
    "detail": "",
}


def get_expected_server_version() -> str:
    env_v = (os.environ.get("XIAOZHI_EXPECTED_SERVER_VERSION") or "").strip()
    return env_v or DEFAULT_EXPECTED_SERVER_VERSION


def reset_registry() -> None:
    """Clear registry state (primarily for isolated unit tests)."""
    with _lock:
        _patches.clear()
        _version_info.update(
            {
                "checked": False,
                "expected": get_expected_server_version(),
                "actual": "",
                "match": True,
                "detail": "",
            }
        )


def check_upstream_server_version(
    expected_version: Optional[str] = None,
    actual_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare upstream config.logger.SERVER_VERSION against expected contract version."""
    expected = (expected_version or get_expected_server_version()).strip()
    actual = (actual_version or "").strip() if actual_version is not None else ""
    detail = ""
    if actual_version is None:
        try:
            import config.logger as _cfg_logger

            actual = str(getattr(_cfg_logger, "SERVER_VERSION", "") or "").strip()
            if not actual:
                detail = "config.logger.SERVER_VERSION missing or empty"
        except Exception as exc:
            detail = f"Failed to import config.logger.SERVER_VERSION: {exc}"
            actual = ""

    match = bool(actual) and actual == expected
    if not match and not detail:
        detail = f"Upstream SERVER_VERSION mismatch: expected {expected!r}, got {actual!r}"

    info = {
        "checked": True,
        "expected": expected,
        "actual": actual,
        "match": match,
        "detail": detail,
    }
    with _lock:
        _version_info.update(info)
    return dict(info)


def verify_signature_contract(
    func: Any,
    *,
    required_params: Iterable[str] = (),
    min_positional: Optional[int] = None,
    require_async: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Inspect a callable's signature to validate upstream compatibility before patching.

    Follows `__wrapped__` if present so idempotent re-checks inspect the underlying contract.
    """
    if func is None or not callable(func):
        return False, "target is None or not callable"

    # Unwrap staticmethod / classmethod descriptors if passed raw from __dict__
    raw_fn = func
    if isinstance(raw_fn, (staticmethod, classmethod)):
        raw_fn = raw_fn.__func__

    if require_async is not None:
        is_coro = inspect.iscoroutinefunction(raw_fn)
        if not is_coro and hasattr(raw_fn, "__wrapped__"):
            is_coro = inspect.iscoroutinefunction(getattr(raw_fn, "__wrapped__"))
        if is_coro != require_async:
            want = "async coroutine" if require_async else "sync callable"
            return False, f"expected {want}, got {'async' if is_coro else 'sync'}"

    try:
        sig = inspect.signature(raw_fn, follow_wrapped=True)
    except (TypeError, ValueError) as exc:
        return False, f"inspect.signature failed: {exc}"

    params = sig.parameters
    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values())
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    req_list = [str(p) for p in required_params if p]
    missing = [
        name
        for name in req_list
        if name not in params and not (has_var_pos and has_var_kw)
    ]
    if missing:
        return (
            False,
            f"missing expected parameter(s) {missing} in signature {sig}",
        )

    if min_positional is not None and not has_var_pos:
        pos_count = sum(
            1
            for p in params.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        if pos_count < int(min_positional):
            return (
                False,
                f"expected at least {min_positional} positional param(s), found {pos_count} in {sig}",
            )

    return True, str(sig)


def record_patch(
    name: str,
    status: str,
    *,
    target: str = "",
    detail: str = "",
    contract_ok: bool = True,
    required: bool = True,
    rebound: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Record the outcome of applying a monkey-patch into the global patch registry."""
    st = str(status or "failed").strip().lower()
    if st not in VALID_PATCH_STATUSES:
        st = "failed"
    entry: Dict[str, Any] = {
        "name": str(name),
        "status": st,
        "target": str(target or ""),
        "detail": str(detail or ""),
        "contract_ok": bool(contract_ok),
        "required": bool(required),
        "rebound": list(rebound) if rebound else [],
        "updated_at": time.time(),
    }
    with _lock:
        _patches[str(name)] = entry
    return dict(entry)


def get_patch_status(name: str) -> Optional[Dict[str, Any]]:
    with _lock:
        entry = _patches.get(name)
        return dict(entry) if entry is not None else None


def get_all_patches() -> Dict[str, Dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _patches.items()}


def get_health_summary() -> Dict[str, Any]:
    """Return structured health & patch contract status for /api/health."""
    with _lock:
        patches_copy = {k: dict(v) for k, v in _patches.items()}
        ver = dict(_version_info)

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    degraded_reasons: List[str] = []

    if ver.get("checked") and not ver.get("match", True):
        degraded_reasons.append(
            ver.get("detail")
            or f"upstream SERVER_VERSION mismatch ({ver.get('actual')} != {ver.get('expected')})"
        )

    for pname, entry in sorted(patches_copy.items()):
        st = entry.get("status", "failed")
        if st in counts:
            counts[st] += 1
        req = bool(entry.get("required", True))
        c_ok = bool(entry.get("contract_ok", True))
        if st == "failed":
            degraded_reasons.append(f"patch {pname} failed: {entry.get('detail') or 'unknown error'}")
        elif not c_ok:
            degraded_reasons.append(
                f"patch {pname} contract mismatch: {entry.get('detail') or 'signature drift'}"
            )
        elif st == "skipped" and req:
            degraded_reasons.append(
                f"required patch {pname} skipped: {entry.get('detail') or 'missing target'}"
            )

    degraded = len(degraded_reasons) > 0
    return {
        "status": "degraded" if degraded else "ok",
        "degraded": degraded,
        "server_version": ver.get("actual", ""),
        "expected_server_version": ver.get("expected", get_expected_server_version()),
        "version_match": bool(ver.get("match", True)),
        "counts": counts,
        "patches": patches_copy,
        "degraded_reasons": degraded_reasons,
    }


def rebind(
    old_obj: Any,
    new_obj: Any,
    *,
    module_prefixes: Optional[Iterable[str]] = ("core.", "config.", "plugins_func."),
    exclude_modules: Iterable[str] = (
        "patch_registry",
        "runtime_events",
        "start_with_env",
        "mei_emotion_router",
        "pc_audio_router",
        "reply_length_router",
        "nmc_weather_patch",
        "console_server",
        "anima_memory_provider",
    ),
) -> List[str]:
    """Replace all module-level references pointing to `old_obj` across `sys.modules` with `new_obj`.

    Protects saved `_orig*` attributes and excludes our own patch modules by default to avoid
    accidentally creating recursion loops when patch wrappers retain references to originals.
    """
    if old_obj is None or new_obj is None or old_obj is new_obj:
        return []

    prefixes = tuple(module_prefixes) if module_prefixes is not None else None
    excluded = set(exclude_modules or ())
    rebound: List[str] = []

    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod_name in excluded:
            continue
        if prefixes is not None and not any(
            mod_name == p.rstrip(".") or mod_name.startswith(p) for p in prefixes
        ):
            continue
        mod_dict = getattr(mod, "__dict__", None)
        if not isinstance(mod_dict, dict):
            continue
        for attr_name, val in list(mod_dict.items()):
            if attr_name.startswith("_orig"):
                continue
            if val is old_obj:
                try:
                    setattr(mod, attr_name, new_obj)
                    rebound.append(f"{mod_name}.{attr_name}")
                except Exception:
                    pass
    return rebound
