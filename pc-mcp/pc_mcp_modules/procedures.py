"""Self-evolving reusable L2 UI procedure storage, validation, versioning & deterministic replay.

Inspired by `dsh-world` procedures:
- Stores validated JSON playbooks in host-private `pc-mcp/state/procedures/<category>/<name>.json`
  with optional per-category `<category>/guide.md` notes and automatic `.v<n>.json` backups.
- Fixed single MCP tool (`pc_ui_procedure`) so newly learned procedures NEVER bloat the tool list
  or invalidate LLM prompt prefix caches.
- Strict schema validation rejects unknown keys (preventing typos from silently changing behavior),
  pre-validates hotkeys against the L2 blacklist, and blocks path traversal / symlink escapes.
- Deterministic zero-VLM replay checks emergency stop on every step, rescales `bg_window` pixel
  coordinates proportionally if the target window was resized, and pauses before any step marked
  `"outward": true` unless `allow_outward=True` is explicitly provided.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import l3_backends

from . import PROCEDURES_DIR as _DEFAULT_PROCEDURES_DIR, _gui_lock, facade_attr
from . import bg_desktop
from .security import (
    _check_hotkey_blacklist,
    _check_permission,
    _parse_and_normalize_hotkey,
    _validate_hotkey_keys,
    _write_l2_audit_log,
    is_emergency_stop_active,
)
from .ui_tools import _pc_ui_action_locked

_ENTRY_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,40}$")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$")
_TEMPLATE_VAR_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_RESERVED_NAMES = frozenset(
    {
        "guide",
        "__proto__",
        "constructor",
        "prototype",
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

_VALID_WHERE = frozenset({"bg_window", "screen"})
_VALID_PARAM_TYPES = frozenset({"string", "number", "boolean"})
_VALID_STEP_OPS = frozenset({"click", "double_click", "right_click", "type_text", "hotkey", "scroll", "wait"})

_TOP_KEYS = frozenset({"name", "category", "description", "where", "window_keyword", "ref_size", "params", "steps"})
_STEP_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "click": frozenset({"op", "x", "y", "outward"}),
    "double_click": frozenset({"op", "x", "y", "outward"}),
    "right_click": frozenset({"op", "x", "y", "outward"}),
    "type_text": frozenset({"op", "text", "x", "y", "press_enter", "outward"}),
    "hotkey": frozenset({"op", "hotkey", "outward"}),
    "scroll": frozenset({"op", "scroll_clicks", "x", "y"}),
    "wait": frozenset({"op", "seconds"}),
}

MAX_STEPS = 30
MAX_STEP_WAIT_SEC = 15.0
MAX_TOTAL_WAIT_SEC = 45.0
MAX_TEXT_LEN = 500
MAX_NOTE_LEN = 1000
MAX_BACKUP_VERSIONS = 5

_proc_lock = threading.RLock()

PROCEDURE_SCHEMA_GUIDE = (
    "【操作剧本 JSON 格式说明】\n"
    '{"category":"分类名","name":"操作名","description":"做什么",'
    '"where":"bg_window|screen","window_keyword":"窗口标题或进程名(bg_window必填)",'
    '"ref_size":[1280,720],"params":{"query":{"type":"string","required":true,"description":"搜索词"}},\n'
    '"steps":[\n'
    '  {"op":"click","x":120,"y":80},\n'
    '  {"op":"type_text","text":"{{query}}","press_enter":true,"outward":false},\n'
    '  {"op":"wait","seconds":1.0},\n'
    '  {"op":"hotkey","hotkey":"ctrl+s"},\n'
    '  {"op":"scroll","scroll_clicks":-5}\n'
    "]}\n"
    "规则：未知字段会被拒绝；最多 30 步；对外发送/提交步骤请标 outward:true（回放默认在执行前暂停确认）。"
)


def _procedures_root() -> Path:
    root = Path(facade_attr("PROCEDURES_DIR", _DEFAULT_PROCEDURES_DIR))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _validate_entry_name(value: str, label: str) -> str:
    s = str(value or "").strip()
    if not s or not _ENTRY_NAME_RE.match(s) or s.lower() in _RESERVED_NAMES:
        raise ValueError(f"{label} 非法（仅允许 1~40 位中英文/数字/_/-，禁止路径符号或保留名）: {value!r}")
    return s


def _safe_child_path(root: Path, *parts: str) -> Path:
    """Resolve child path under `root` and fail-closed on path traversal or symlinks."""
    cur = root
    for part in parts:
        cand = cur / part
        if cand.is_symlink():
            raise ValueError(f"拒绝访问符号链接路径: {part}")
        cur = cand
    resolved = cur.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("检测到非法越界路径，已拒绝访问") from exc
    return resolved


def _check_unknown_keys(obj: dict, allowed: frozenset[str], path: str) -> None:
    extra = sorted(str(k) for k in obj.keys() if k not in allowed)
    if extra:
        raise ValueError(f"{path} 包含未识别字段: {', '.join(extra)}")


def _require_int(val: Any, label: str, *, lo: int = -10000, hi: int = 32768) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{label} 必须为整数")
    if val < lo or val > hi:
        raise ValueError(f"{label} 超出范围 ({lo}..{hi})")
    return val


def validate_procedure_spec(raw: Any) -> Dict[str, Any]:
    """Validate and normalize a procedure JSON definition (fail-closed on unknown keys or blocked hotkeys)."""
    if not isinstance(raw, dict):
        raise ValueError("剧本必须为 JSON 对象")
    _check_unknown_keys(raw, _TOP_KEYS, "剧本顶层")

    category = _validate_entry_name(str(raw.get("category") or ""), "category")
    name = _validate_entry_name(str(raw.get("name") or ""), "name")
    desc = str(raw.get("description") or "").strip()[:200]
    where = str(raw.get("where") or "").strip().lower()
    if where not in _VALID_WHERE:
        raise ValueError("where 必须为 'bg_window' 或 'screen'")

    win_kw = str(raw.get("window_keyword") or "").strip()[:120]
    if where == "bg_window" and not win_kw:
        raise ValueError("where='bg_window' 时必须提供 window_keyword")

    ref_size: Optional[List[int]] = None
    if "ref_size" in raw and raw["ref_size"] is not None:
        rs = raw["ref_size"]
        if not isinstance(rs, list) or len(rs) != 2:
            raise ValueError("ref_size 必须为 [width, height] 二元整数数组")
        rw = _require_int(rs[0], "ref_size[0]", lo=10, hi=16384)
        rh = _require_int(rs[1], "ref_size[1]", lo=10, hi=16384)
        ref_size = [rw, rh]

    params_raw = raw.get("params") or {}
    if not isinstance(params_raw, dict):
        raise ValueError("params 必须为对象")
    params_clean: Dict[str, Dict[str, Any]] = {}
    for pname, pspec in params_raw.items():
        if not isinstance(pname, str) or not _PARAM_NAME_RE.match(pname) or pname in _RESERVED_NAMES:
            raise ValueError(f"非法参数名: {pname!r}")
        if not isinstance(pspec, dict):
            raise ValueError(f"params.{pname} 必须为对象")
        _check_unknown_keys(pspec, frozenset({"type", "required", "description", "default"}), f"params.{pname}")
        ptype = str(pspec.get("type") or "string").strip().lower()
        if ptype not in _VALID_PARAM_TYPES:
            raise ValueError(f"params.{pname}.type 必须为 string/number/boolean")
        req_raw = pspec.get("required", False)
        if not isinstance(req_raw, bool):
            raise ValueError(f"params.{pname}.required 必须为布尔值")
        req = req_raw
        pdesc = str(pspec.get("description") or "").strip()[:120]
        entry: Dict[str, Any] = {"type": ptype, "required": req, "description": pdesc}
        if "default" in pspec:
            dval = pspec["default"]
            if ptype == "boolean" and not isinstance(dval, bool):
                raise ValueError(f"params.{pname}.default 必须为布尔值")
            if ptype == "number" and (isinstance(dval, bool) or not isinstance(dval, (int, float)) or not math.isfinite(float(dval))):
                raise ValueError(f"params.{pname}.default 必须为有限数字")
            if ptype == "string" and (not isinstance(dval, str) or len(dval) > MAX_TEXT_LEN):
                raise ValueError(f"params.{pname}.default 必须为不超过 {MAX_TEXT_LEN} 字的字符串")
            entry["default"] = dval
        params_clean[pname] = entry

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("steps 必须为非空数组")
    if len(steps_raw) > MAX_STEPS:
        raise ValueError(f"steps 数量超出上限（最多 {MAX_STEPS} 步）")

    steps_clean: List[Dict[str, Any]] = []
    total_wait = 0.0
    used_vars: set[str] = set(_TEMPLATE_VAR_RE.findall(win_kw))

    for idx, step in enumerate(steps_raw):
        spath = f"steps[{idx}]"
        if not isinstance(step, dict):
            raise ValueError(f"{spath} 必须为对象")
        op = str(step.get("op") or "").strip().lower()
        if op not in _VALID_STEP_OPS:
            raise ValueError(f"{spath}.op 不支持: {op!r}")
        _check_unknown_keys(step, _STEP_ALLOWED_KEYS[op], spath)

        outward = step.get("outward", False)
        if not isinstance(outward, bool):
            raise ValueError(f"{spath}.outward 必须为布尔值")

        coord_lo = 0 if where == "bg_window" else -10000
        if op in ("click", "double_click", "right_click"):
            sx = _require_int(step.get("x"), f"{spath}.x", lo=coord_lo)
            sy = _require_int(step.get("y"), f"{spath}.y", lo=coord_lo)
            clean_step: Dict[str, Any] = {"op": op, "x": sx, "y": sy}
            if outward:
                clean_step["outward"] = True
            steps_clean.append(clean_step)
        elif op == "type_text":
            txt = step.get("text")
            if not isinstance(txt, str) or not txt:
                raise ValueError(f"{spath}.text 必须为非空字符串")
            if len(txt) > MAX_TEXT_LEN:
                raise ValueError(f"{spath}.text 超出最大长度 {MAX_TEXT_LEN}")
            used_vars.update(_TEMPLATE_VAR_RE.findall(txt))
            clean_step = {"op": "type_text", "text": txt}
            if "x" in step or "y" in step:
                clean_step["x"] = _require_int(step.get("x"), f"{spath}.x", lo=coord_lo)
                clean_step["y"] = _require_int(step.get("y"), f"{spath}.y", lo=coord_lo)
            if "press_enter" in step:
                if not isinstance(step["press_enter"], bool):
                    raise ValueError(f"{spath}.press_enter 必须为布尔值")
                clean_step["press_enter"] = step["press_enter"]
            if outward:
                clean_step["outward"] = True
            steps_clean.append(clean_step)
        elif op == "hotkey":
            hk = step.get("hotkey")
            if not isinstance(hk, str) or not hk.strip():
                raise ValueError(f"{spath}.hotkey 必须为非空字符串")
            exec_keys, canon_keys = _parse_and_normalize_hotkey(hk)
            if not exec_keys:
                raise ValueError(f"{spath}.hotkey 无法解析: {hk!r}")
            hk_err = _check_hotkey_blacklist(canon_keys, hk)
            if hk_err:
                raise ValueError(f"{spath}.hotkey 命中安全黑名单: {hk_err}")
            inv = _validate_hotkey_keys(exec_keys)
            if inv:
                raise ValueError(f"{spath}.hotkey 包含未识别按键: {', '.join(inv)}")
            if where == "bg_window":
                unsupported_bg = [k for k in canon_keys if k.lower() not in bg_desktop._VK_MAP]
                if unsupported_bg:
                    raise ValueError(f"{spath}.hotkey 包含后台模式不支持的按键: {', '.join(unsupported_bg)}")
            clean_step = {"op": "hotkey", "hotkey": "+".join(canon_keys)}
            if outward:
                clean_step["outward"] = True
            steps_clean.append(clean_step)
        elif op == "scroll":
            clicks = _require_int(step.get("scroll_clicks", -5), f"{spath}.scroll_clicks", lo=-50, hi=50)
            clean_step = {"op": "scroll", "scroll_clicks": clicks}
            if "x" in step or "y" in step:
                clean_step["x"] = _require_int(step.get("x"), f"{spath}.x", lo=coord_lo)
                clean_step["y"] = _require_int(step.get("y"), f"{spath}.y", lo=coord_lo)
            steps_clean.append(clean_step)
        elif op == "wait":
            raw_sec = step.get("seconds")
            if isinstance(raw_sec, bool) or not isinstance(raw_sec, (int, float)):
                raise ValueError(f"{spath}.seconds 必须为数字")
            sec = float(raw_sec)
            if sec != sec or sec < 0.05 or sec > MAX_STEP_WAIT_SEC:
                raise ValueError(f"{spath}.seconds 必须在 0.05..{MAX_STEP_WAIT_SEC} 秒之间")
            total_wait += sec
            if total_wait > MAX_TOTAL_WAIT_SEC:
                raise ValueError(f"剧本累计 wait 时长超过上限 ({MAX_TOTAL_WAIT_SEC}s)")
            steps_clean.append({"op": "wait", "seconds": round(sec, 2)})

    undeclared = sorted(v for v in used_vars if v not in params_clean)
    if undeclared:
        raise ValueError(f"模板引用了未在 params 中声明的变量: {', '.join(undeclared)}")

    normalized: Dict[str, Any] = {
        "category": category,
        "name": name,
        "description": desc,
        "where": where,
        "params": params_clean,
        "steps": steps_clean,
    }
    if win_kw:
        normalized["window_keyword"] = win_kw
    if ref_size is not None:
        normalized["ref_size"] = ref_size
    return normalized


def _backup_versions(cat_dir: Path, name: str) -> List[Tuple[int, Path]]:
    pat = re.compile(rf"^{re.escape(name)}\.v(\d+)\.json$", re.IGNORECASE)
    found: List[Tuple[int, Path]] = []
    if not cat_dir.is_dir():
        return found
    for fp in cat_dir.iterdir():
        if fp.is_symlink() or not fp.is_file():
            continue
        m = pat.match(fp.name)
        if m:
            found.append((int(m.group(1)), fp))
    found.sort(key=lambda pair: pair[0])
    return found


def save_procedure(raw: Any) -> Dict[str, Any]:
    spec = validate_procedure_spec(raw)
    root = _procedures_root()
    cat_dir = _safe_child_path(root, spec["category"])
    cat_dir.mkdir(parents=True, exist_ok=True)
    target = _safe_child_path(root, spec["category"], f"{spec['name']}.json")

    l3_mod = facade_attr("l3_backends", l3_backends)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    backed_up = ""
    with _proc_lock:
        if target.is_file():
            existing_vers = _backup_versions(cat_dir, spec["name"])
            next_v = (existing_vers[-1][0] + 1) if existing_vers else 1
            bak = _safe_child_path(root, spec["category"], f"{spec['name']}.v{next_v}.json")
            l3_mod.atomic_write_text(bak, target.read_text(encoding="utf-8"), encoding="utf-8")
            backed_up = bak.name
            all_vers = existing_vers + [(next_v, bak)]
            if len(all_vers) > MAX_BACKUP_VERSIONS:
                for _, old_fp in all_vers[: len(all_vers) - MAX_BACKUP_VERSIONS]:
                    try:
                        old_fp.unlink()
                    except OSError:
                        pass
        l3_mod.atomic_write_json(target, spec)
    audit_fn(
        tool="pc_ui_procedure",
        action="save",
        outcome="allowed",
        reason="ok",
        target=f"{spec['category']}/{spec['name']}",
        detail=f"steps={len(spec['steps'])} where={spec['where']} backup={backed_up}",
    )
    return {"ok": True, "path": f"{spec['category']}/{spec['name']}", "backed_up": backed_up, "spec": spec}


def rollback_procedure(category: str, name: str) -> Dict[str, Any]:
    cat = _validate_entry_name(category, "category")
    nm = _validate_entry_name(name, "name")
    root = _procedures_root()
    cat_dir = _safe_child_path(root, cat)
    target = _safe_child_path(root, cat, f"{nm}.json")
    l3_mod = facade_attr("l3_backends", l3_backends)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    with _proc_lock:
        vers = _backup_versions(cat_dir, nm)
        if not vers:
            raise ValueError(f"剧本 {cat}/{nm} 没有可回退的历史版本")
        ver_num, bak_fp = vers[-1]
        data = json.loads(bak_fp.read_text(encoding="utf-8"))
        spec = validate_procedure_spec(data)
        l3_mod.atomic_write_json(target, spec)
        try:
            bak_fp.unlink()
        except OSError:
            pass
    audit_fn(
        tool="pc_ui_procedure",
        action="rollback",
        outcome="allowed",
        reason="ok",
        target=f"{cat}/{nm}",
        detail=f"restored=v{ver_num}",
    )
    return {"ok": True, "path": f"{cat}/{nm}", "restored_version": ver_num}


def get_procedure(category: str, name: str) -> Dict[str, Any]:
    cat = _validate_entry_name(category, "category")
    nm = _validate_entry_name(name, "name")
    root = _procedures_root()
    with _proc_lock:
        target = _safe_child_path(root, cat, f"{nm}.json")
        if not target.is_file():
            raise FileNotFoundError(f"未找到剧本 {cat}/{nm}")
        return validate_procedure_spec(json.loads(target.read_text(encoding="utf-8")))


def delete_procedure(category: str, name: str) -> bool:
    cat = _validate_entry_name(category, "category")
    nm = _validate_entry_name(name, "name")
    root = _procedures_root()
    cat_dir = _safe_child_path(root, cat)
    target = _safe_child_path(root, cat, f"{nm}.json")
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    with _proc_lock:
        existed = target.is_file()
        if existed:
            target.unlink()
        # Also remove backup versions so deleted procedures cannot be resurrected via rollback
        for _, bak_fp in _backup_versions(cat_dir, nm):
            try:
                bak_fp.unlink()
            except OSError:
                pass
    if existed:
        audit_fn(
            tool="pc_ui_procedure",
            action="delete",
            outcome="allowed",
            reason="ok",
            target=f"{cat}/{nm}",
        )
    return existed


def save_category_note(category: str, note: str) -> str:
    cat = _validate_entry_name(category, "category")
    root = _procedures_root()
    cat_dir = _safe_child_path(root, cat)
    cat_dir.mkdir(parents=True, exist_ok=True)
    guide = _safe_child_path(root, cat, "guide.md")
    clean = str(note or "").strip()[:MAX_NOTE_LEN]
    l3_mod = facade_attr("l3_backends", l3_backends)
    with _proc_lock:
        l3_mod.atomic_write_text(guide, clean + "\n", encoding="utf-8")
    return clean


def _sorted_procedure_stems(cat_dir: Path) -> List[str]:
    entries: List[Tuple[float, str]] = []
    for fp in cat_dir.glob("*.json"):
        if fp.is_symlink() or re.search(r"\.v\d+\.json$", fp.name, re.IGNORECASE):
            continue
        try:
            mt = float(fp.stat().st_mtime)
        except OSError:
            continue
        entries.append((mt, fp.stem))
    entries.sort(key=lambda pair: pair[0], reverse=True)
    return [stem for _, stem in entries]


def list_procedures(category: str = "") -> Dict[str, Any]:
    root = _procedures_root()
    with _proc_lock:
        if category.strip():
            cat = _validate_entry_name(category, "category")
            cat_dir = _safe_child_path(root, cat)
            if not cat_dir.is_dir():
                return {"category": cat, "note": "", "procedures": []}
            guide = cat_dir / "guide.md"
            note = (
                guide.read_text(encoding="utf-8", errors="ignore").strip()[:500]
                if guide.is_file() and not guide.is_symlink()
                else ""
            )
            return {"category": cat, "note": note, "procedures": _sorted_procedure_stems(cat_dir)}

        cats: Dict[str, List[str]] = {}
        for sub in sorted(root.iterdir()):
            if not sub.is_dir() or sub.is_symlink() or not _ENTRY_NAME_RE.match(sub.name):
                continue
            cats[sub.name] = _sorted_procedure_stems(sub)
        return {"categories": cats}


def _bind_args(params_spec: Dict[str, Dict[str, Any]], raw_args: Dict[str, Any]) -> Dict[str, str]:
    bound: Dict[str, str] = {}
    for pname, pspec in params_spec.items():
        if pname in raw_args and raw_args[pname] is not None:
            val = raw_args[pname]
        elif "default" in pspec:
            val = pspec["default"]
        elif pspec.get("required"):
            raise ValueError(f"缺少必填参数: {pname}")
        else:
            val = ""
        ptype = pspec.get("type", "string")
        if ptype == "boolean":
            if not isinstance(val, bool):
                raise ValueError(f"参数 {pname} 必须为布尔值")
            bound[pname] = "true" if val else "false"
        elif ptype == "number":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                raise ValueError(f"参数 {pname} 必须为有限数字")
            bound[pname] = str(val)
        else:
            bound[pname] = str(val)[:MAX_TEXT_LEN]
    return bound


def _substitute(template: str, bound: Dict[str, str], *, max_len: int = MAX_TEXT_LEN) -> str:
    rendered = _TEMPLATE_VAR_RE.sub(lambda m: bound.get(m.group(1), ""), template)
    return rendered[:max_len]


def _scale_xy(x: int, y: int, ref_size: Optional[List[int]], cur_size: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    if not ref_size or not cur_size or ref_size[0] <= 0 or ref_size[1] <= 0:
        return int(x), int(y)
    sx = int(round(x * (cur_size[0] / float(ref_size[0]))))
    sy = int(round(y * (cur_size[1] / float(ref_size[1]))))
    return max(0, min(cur_size[0] - 1, sx)), max(0, min(cur_size[1] - 1, sy))


def _interruptible_sleep(seconds: float, estop_fn, tmod) -> bool:
    """Sleep in <=0.1s slices; returns False if emergency stop triggered mid-wait."""
    remaining = max(0.0, float(seconds))
    while remaining > 0.0:
        if estop_fn():
            return False
        step = min(0.1, remaining)
        tmod.sleep(step)
        remaining -= step
    return not bool(estop_fn())


def run_procedure(
    category: str,
    name: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    allow_outward: bool = False,
) -> str:
    """Deterministically replay a saved procedure without invoking any LLM."""
    perm_fn = facade_attr("_check_permission", _check_permission)
    estop_fn = facade_attr("is_emergency_stop_active", is_emergency_stop_active)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    tmod = facade_attr("time", time)

    perm_err = perm_fn("L2")
    if perm_err:
        audit_fn(
            tool="pc_ui_procedure",
            action="run",
            outcome="blocked",
            reason="emergency_stop" if estop_fn() else "mode_blocked",
            target=f"{category}/{name}",
            detail=perm_err,
        )
        return perm_err

    spec = get_procedure(category, name)
    bound = _bind_args(spec.get("params") or {}, args or {})
    where = spec["where"]
    win_kw = _substitute(str(spec.get("window_keyword") or ""), bound, max_len=120)
    ref_size = spec.get("ref_size")

    locked_hwnd = 0
    cur_client_size: Optional[Tuple[int, int]] = None
    if where == "bg_window":
        win = bg_desktop.find_bg_window(win_kw)
        if not win:
            return f"回放中止：未找到后台目标窗口「{win_kw}」。"
        locked_hwnd = int(win["hwnd"])
        _, guard_err = bg_desktop.check_bg_window_guard(locked_hwnd)
        if guard_err:
            audit_fn(
                tool="pc_ui_procedure",
                action="run",
                outcome="blocked",
                reason="bg_window_blocked",
                target=f"{category}/{name}",
                detail=guard_err,
            )
            return f"回放中止：{guard_err}"
        try:
            with _gui_lock:
                _, cur_client_size = facade_attr("capture_window_client", bg_desktop.capture_window_client)(locked_hwnd)
        except Exception:
            cur_client_size = None

    steps = spec["steps"]
    executed = 0
    last_msg = ""
    for idx, step in enumerate(steps):
        if estop_fn():
            audit_fn(
                tool="pc_ui_procedure",
                action="run",
                outcome="blocked",
                reason="emergency_stop",
                target=f"{category}/{name}",
                detail=f"aborted at step {idx + 1}/{len(steps)}",
            )
            return f"安全急停已触发，剧本 {category}/{name} 在第 {idx + 1}/{len(steps)} 步中止。"

        op = step["op"]
        if step.get("outward") and not allow_outward:
            audit_fn(
                tool="pc_ui_procedure",
                action="run",
                outcome="blocked",
                reason="outward_confirmation_required",
                target=f"{category}/{name}",
                detail=f"paused before outward step {idx + 1}: {op}",
            )
            return (
                f"剧本 {category}/{name} 已执行 {executed}/{len(steps)} 步，在第 {idx + 1} 步（{op}）处暂停："
                "该步骤标记为 outward（对外发送/提交动作），请确认后带 allow_outward=True 再执行。"
            )

        if op == "wait":
            if not _interruptible_sleep(float(step["seconds"]), estop_fn, tmod):
                audit_fn(
                    tool="pc_ui_procedure",
                    action="run",
                    outcome="blocked",
                    reason="emergency_stop",
                    target=f"{category}/{name}",
                    detail=f"aborted during wait step {idx + 1}/{len(steps)}",
                )
                return f"安全急停已触发，剧本 {category}/{name} 在第 {idx + 1}/{len(steps)} 步等待期间中止。"
            executed += 1
            continue

        step_ok = True
        if where == "bg_window":
            sx = int(step["x"]) if "x" in step else -1
            sy = int(step["y"]) if "y" in step else -1
            if sx >= 0 and sy >= 0 and cur_client_size is not None:
                sx, sy = _scale_xy(sx, sy, ref_size, cur_client_size)
            step_ok, last_msg = bg_desktop._execute_bg_window_action_result(
                action=op,
                window_keyword=win_kw,
                x=sx,
                y=sy,
                text=_substitute(str(step.get("text") or ""), bound) if op == "type_text" else "",
                press_enter=bool(step.get("press_enter", False)),
                hotkey=str(step.get("hotkey") or ""),
                scroll_clicks=int(step.get("scroll_clicks", -3)),
                target_hwnd=locked_hwnd,
            )
        else:
            sx = int(step.get("x", -1))
            sy = int(step.get("y", -1))
            if op == "type_text" and "x" in step and "y" in step:
                clk_msg = _pc_ui_action_locked(act="click", raw_action="click", x=sx, y=sy)
                if not clk_msg.startswith("已"):
                    step_ok, last_msg = False, clk_msg
                else:
                    tmod.sleep(0.08)
                    last_msg = _pc_ui_action_locked(
                        act="type_text",
                        raw_action="type_text",
                        text=_substitute(str(step.get("text") or ""), bound),
                        press_enter=bool(step.get("press_enter", False)),
                    )
                    step_ok = last_msg.startswith("已")
            elif op == "scroll" and "x" in step and "y" in step:
                clk_msg = _pc_ui_action_locked(act="click", raw_action="click", x=sx, y=sy)
                if not clk_msg.startswith("已"):
                    step_ok, last_msg = False, clk_msg
                else:
                    last_msg = _pc_ui_action_locked(
                        act="scroll",
                        raw_action="scroll",
                        scroll_clicks=int(step.get("scroll_clicks", -5)),
                    )
                    step_ok = last_msg.startswith("已")
            else:
                last_msg = _pc_ui_action_locked(
                    act=op,
                    raw_action=op,
                    x=sx,
                    y=sy,
                    text=_substitute(str(step.get("text") or ""), bound) if op == "type_text" else "",
                    press_enter=bool(step.get("press_enter", False)),
                    hotkey=str(step.get("hotkey") or ""),
                    scroll_clicks=int(step.get("scroll_clicks", -5)),
                )
                step_ok = last_msg.startswith("已")

        if not step_ok:
            audit_fn(
                tool="pc_ui_procedure",
                action="run",
                outcome="blocked",
                reason="step_failed",
                target=f"{category}/{name}",
                detail=f"step {idx + 1}/{len(steps)} ({op}): {last_msg[:120]}",
            )
            return f"剧本 {category}/{name} 在第 {idx + 1}/{len(steps)} 步（{op}）中断：{last_msg}"
        executed += 1

    audit_fn(
        tool="pc_ui_procedure",
        action="run",
        outcome="allowed",
        reason="ok",
        target=f"{category}/{name}",
        detail=f"steps={executed}/{len(steps)} where={where}",
    )
    return f"已完成剧本 {category}/{name}（共 {executed}/{len(steps)} 步，模式={where}）。末步反馈：{last_msg}"


def handle_procedure_tool(
    *,
    action: str,
    category: str = "",
    name: str = "",
    procedure_json: str = "",
    args_json: str = "",
    note: str = "",
    allow_outward: bool = False,
) -> str:
    """MCP entrypoint for `pc_ui_procedure`."""
    act = (action or "list").strip().lower()
    perm_fn = facade_attr("_check_permission", _check_permission)
    perm_err = perm_fn("L2")
    if perm_err:
        return perm_err

    try:
        if act == "list":
            res = list_procedures(category)
            if "category" in res:
                procs = ", ".join(res["procedures"]) or "（暂无剧本）"
                note_part = f"\n说明：{res['note']}" if res["note"] else ""
                return f"分类「{res['category']}」操作剧本：{procs}{note_part}"
            cats = res.get("categories") or {}
            if not cats:
                return "当前尚未保存任何 L2 操作剧本。调用 action='save'（不传 procedure_json）可查看编写格式。"
            lines = [f"- {c}: {', '.join(ps) or '（空）'}" for c, ps in cats.items()]
            return "已保存的操作剧本分类：\n" + "\n".join(lines)

        if act == "get":
            spec = get_procedure(category, name)
            return json.dumps(spec, ensure_ascii=False, indent=2)

        if act == "save":
            if not (procedure_json or "").strip():
                return PROCEDURE_SCHEMA_GUIDE
            raw_obj = json.loads(procedure_json)
            if isinstance(raw_obj, dict):
                if category.strip() and "category" not in raw_obj:
                    raw_obj["category"] = category.strip()
                if name.strip() and "name" not in raw_obj:
                    raw_obj["name"] = name.strip()
            saved = save_procedure(raw_obj)
            bak_tip = f"（旧版已备份为 {saved['backed_up']}，可用 rollback 回退）" if saved["backed_up"] else ""
            return f"已保存操作剧本 {saved['path']}{bak_tip}。"

        if act == "rollback":
            rb = rollback_procedure(category, name)
            return f"已将剧本 {rb['path']} 回退至历史版本 v{rb['restored_version']}。"

        if act == "note":
            cleaned = save_category_note(category, note)
            return f"已更新分类「{category.strip()}」说明（{len(cleaned)} 字）。"

        if act == "delete":
            ok = delete_procedure(category, name)
            return f"已删除剧本 {category}/{name}。" if ok else f"未找到剧本 {category}/{name}。"

        if act == "run":
            parsed_args: Dict[str, Any] = {}
            if (args_json or "").strip():
                obj = json.loads(args_json)
                if not isinstance(obj, dict):
                    raise ValueError("args_json 必须为 JSON 对象")
                parsed_args = obj
            return run_procedure(category, name, parsed_args, allow_outward=bool(allow_outward))

        return f"不支持的剧本动作: {action}（支持 list / get / save / run / rollback / note / delete）"
    except Exception as exc:
        return f"操作剧本执行失败：{exc}"
