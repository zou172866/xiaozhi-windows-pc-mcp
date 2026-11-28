"""Persistent natural-language future tasks & lightweight Cron scheduler (Phase 1).

Zero import-time side effects: no SQLite files, no background threads, no event subscriptions
until `start_future_tasks()` or an engine method is explicitly called.

Design invariants:
- Pure stdlib 5-field Cron parser (`min hour dom month dow`) in local timezone (`TZ=Asia/Shanghai`).
- SQLite WAL persistence in `state/future_tasks.sqlite3` (`busy_timeout=5000`).
- Restart storm guard: one-shot tasks overdue by > STALE_GRACE_SECONDS (300s) are marked `missed`
  instead of blasting stale alarms on boot; cron tasks advance directly to the next future slot.
- Hard safety bounds: max 50 active tasks, min cron interval >= 5 minutes, text <= 200 chars,
  mandatory `recall_guard` + secret redaction before delivery.
- Delivery adapters reuse existing `proactive_companion` TTS + `18900` PC speaker, `mobile_push`,
  and optional `qq_bot_bridge` (Phase 3) with clean degradation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

import recall_guard
import runtime_events
import runtime_settings
import xz_common

DB_PATH = os.path.join(xz_common.STATE_DIR, "future_tasks.sqlite3")
MAX_ACTIVE_TASKS = 50
MIN_CRON_INTERVAL_MINUTES = 5
MAX_TITLE_CHARS = 80
MAX_MESSAGE_CHARS = 200
MAX_RUN_HISTORY = 80
STALE_GRACE_SECONDS = 300.0
BUSY_DEFER_SECONDS = 20.0
MAX_BUSY_DEFERS = 3
POLL_INTERVAL_SECONDS = 15.0

VALID_TASK_KINDS = frozenset({"reminder", "weather_briefing"})
VALID_SCHEDULE_TYPES = frozenset({"once", "cron"})
VALID_DELIVERIES = frozenset({"pc_speaker", "mobile_push", "pc_and_mobile", "qq_bot", "dry_run"})
VALID_STATUSES = frozenset({"active", "paused", "completed", "cancelled", "missed"})

_SECRET_REDACT_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"(?i)\b(bearer|token|apikey|api_key|secret|password)\s*[:=]\s*[^\s,;\"']{6,}"), r"\1=[已脱敏]"),
    (re.compile(r"(?i)([?&](?:key|token|access_token|secret)=)[^&\s]+"), r"\1[已脱敏]"),
)
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_text(raw: Any, max_len: int) -> str:
    txt = _CTRL_CHARS_RE.sub("", str(raw or "")).strip()
    txt = re.sub(r"\s+", " ", txt)
    for pat, repl in _SECRET_REDACT_PATTERNS:
        txt = pat.sub(repl, txt)
    try:
        sanitized, _ = recall_guard.sanitize_text_for_llm(txt, enabled=True)
        txt = sanitized.strip()
    except Exception:
        pass
    if len(txt) > max_len:
        txt = txt[: max(1, max_len - 1)].rstrip() + "…"
    return txt


# ---------------------------------------------------------------------------
# Pure-stdlib 5-field Cron parser (`minute hour day_of_month month day_of_week`)
# ---------------------------------------------------------------------------

def _parse_cron_field(token: str, lo: int, hi: int, *, allow_seven_as_zero: bool = False) -> Tuple[Set[int], bool]:
    raw = str(token or "").strip()
    if not raw:
        raise ValueError("Cron 字段不能为空")
    is_wildcard = raw == "*"
    out: Set[int] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p:
            raise ValueError(f"Cron 字段语法非法: {token!r}")
        step = 1
        if "/" in p:
            pieces = p.split("/")
            if len(pieces) != 2 or not pieces[1].isdigit():
                raise ValueError(f"Cron 步长非法: {part!r}")
            step = int(pieces[1])
            if step <= 0:
                raise ValueError(f"Cron 步长必须大于 0: {part!r}")
            p = pieces[0].strip()

        if p == "*":
            start, end = lo, hi
        elif "-" in p:
            bounds = p.split("-")
            if len(bounds) != 2 or not bounds[0].isdigit() or not bounds[1].isdigit():
                raise ValueError(f"Cron 范围非法: {part!r}")
            start, end = int(bounds[0]), int(bounds[1])
            if allow_seven_as_zero and end == 7 and start <= 7:
                # Support ranges like 5-7 (Fri-Sun)
                pass
            elif start > end:
                raise ValueError(f"Cron 范围起始大于结束: {part!r}")
        else:
            if not p.isdigit():
                raise ValueError(f"Cron 数值非法: {part!r}")
            start = end = int(p)

        max_allowed = 7 if allow_seven_as_zero else hi
        if start < lo or end > max_allowed or start > end:
            raise ValueError(f"Cron 数值越界 ({lo}-{max_allowed}): {part!r}")

        for val in range(start, end + 1, step):
            out.add(0 if (allow_seven_as_zero and val == 7) else val)

    if not out:
        raise ValueError(f"Cron 字段未匹配任何有效值: {token!r}")
    return out, is_wildcard


def parse_cron_expr(expr: str) -> Dict[str, Any]:
    """Parse standard 5-field cron expression: `minute hour day_of_month month day_of_week`."""
    parts = str(expr or "").strip().split()
    if len(parts) != 5:
        raise ValueError("Cron 表达式必须包含 5 段：分 时 日 月 周（例如 `0 8 * * *`）")
    minutes, _ = _parse_cron_field(parts[0], 0, 59)
    hours, _ = _parse_cron_field(parts[1], 0, 23)
    doms, dom_wild = _parse_cron_field(parts[2], 1, 31)
    months, _ = _parse_cron_field(parts[3], 1, 12)
    dows, dow_wild = _parse_cron_field(parts[4], 0, 6, allow_seven_as_zero=True)
    return {
        "expr": " ".join(parts),
        "minutes": sorted(minutes),
        "hours": sorted(hours),
        "doms": doms,
        "dom_wild": dom_wild,
        "months": sorted(months),
        "dows": dows,
        "dow_wild": dow_wild,
    }


def _cron_day_matches(dt: datetime, parsed: Dict[str, Any]) -> bool:
    # Python weekday(): Monday=0..Sunday=6 -> Cron dow: Sunday=0, Monday=1..Saturday=6
    cron_dow = (dt.weekday() + 1) % 7
    dom_ok = dt.day in parsed["doms"]
    dow_ok = cron_dow in parsed["dows"]
    if parsed["dom_wild"] and parsed["dow_wild"]:
        return True
    if parsed["dom_wild"]:
        return dow_ok
    if parsed["dow_wild"]:
        return dom_ok
    return dom_ok or dow_ok


def next_cron_datetime(expr: str, after_dt: datetime) -> datetime:
    """Compute the strictly next local datetime matching a 5-field cron expression."""
    parsed = parse_cron_expr(expr)
    # Start at the next whole minute strictly after `after_dt`
    cand = (after_dt.replace(second=0, microsecond=0)) + timedelta(minutes=1)
    limit = cand + timedelta(days=366 * 4 + 2)
    months_set = set(parsed["months"])
    hours_set = set(parsed["hours"])
    minutes_list = parsed["minutes"]
    hours_list = parsed["hours"]

    while cand <= limit:
        if cand.month not in months_set:
            if cand.month == 12:
                cand = cand.replace(year=cand.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                cand = cand.replace(month=cand.month + 1, day=1, hour=0, minute=0)
            continue

        if not _cron_day_matches(cand, parsed):
            cand = (cand + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        if cand.hour not in hours_set:
            next_h = next((h for h in hours_list if h > cand.hour), None)
            if next_h is None:
                cand = (cand + timedelta(days=1)).replace(hour=0, minute=0)
            else:
                cand = cand.replace(hour=next_h, minute=0)
            continue

        next_m = next((m for m in minutes_list if m >= cand.minute), None)
        if next_m is None:
            next_h = next((h for h in hours_list if h > cand.hour), None)
            if next_h is None:
                cand = (cand + timedelta(days=1)).replace(hour=0, minute=0)
            else:
                cand = cand.replace(hour=next_h, minute=0)
            continue

        return cand.replace(minute=next_m, second=0, microsecond=0)

    raise ValueError(f"Cron 表达式在未来 4 年内无有效触发日期: {expr!r}")


def validate_cron_min_interval(
    expr: str,
    *,
    min_minutes: int = MIN_CRON_INTERVAL_MINUTES,
    ref_dt: Optional[datetime] = None,
) -> str:
    """Validate syntax and reject cron expressions that fire more often than `min_minutes`."""
    parsed = parse_cron_expr(expr)
    cur = ref_dt or datetime.now().astimezone()
    prev: Optional[datetime] = None
    for _ in range(6):
        nxt = next_cron_datetime(parsed["expr"], cur)
        if prev is not None:
            gap_min = (nxt - prev).total_seconds() / 60.0
            if gap_min < float(min_minutes):
                raise ValueError(
                    f"周期任务间隔过短（{gap_min:.1f} 分钟），为避免频繁打扰最小间隔须 >= {min_minutes} 分钟"
                )
        prev = nxt
        cur = nxt
    return parsed["expr"]


def parse_run_at_input(
    run_at: Any,
    *,
    now_dt: datetime,
) -> datetime:
    """Parse ISO-8601 / `YYYY-MM-DD HH:MM` / `HH:MM` / unix epoch into timezone-aware local datetime."""
    tz = now_dt.tzinfo
    if isinstance(run_at, (int, float)) and not isinstance(run_at, bool):
        return datetime.fromtimestamp(float(run_at), tz=tz)
    s = str(run_at or "").strip()
    if not s:
        raise ValueError("一次性任务请提供 run_at 或 delay_minutes")

    # HH:MM (today if still in the future, otherwise tomorrow)
    m_hm = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$", s)
    if m_hm:
        hh, mm, ss = int(m_hm.group(1)), int(m_hm.group(2)), int(m_hm.group(3) or 0)
        dt = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if dt <= now_dt:
            dt = dt + timedelta(days=1)
        return dt

    # Replace space with T for fromisoformat compatibility
    norm = s.replace("/", "-").replace(" ", "T")
    if norm.endswith("Z"):
        norm = norm[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError as exc:
        raise ValueError(
            f"无法解析触发时间 {s!r}，请使用 YYYY-MM-DD HH:MM、HH:MM 或 ISO8601 格式"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt


# ---------------------------------------------------------------------------
# Default weather & character helpers (lazy-imported so import is side-effect free)
# ---------------------------------------------------------------------------

def _is_in_quiet_hours(now_dt: datetime, quiet_start: str, quiet_end: str) -> bool:
    try:
        sh, sm = [int(x) for x in str(quiet_start or "23:00").strip().split(":", 1)]
        eh, em = [int(x) for x in str(quiet_end or "08:00").strip().split(":", 1)]
    except Exception:
        return False
    cur_m = now_dt.hour * 60 + now_dt.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= cur_m < end_m
    return cur_m >= start_m or cur_m < end_m


def _lazy_proactive_attr(name: str, fallback: Any) -> Any:
    try:
        import proactive_companion

        return getattr(proactive_companion, name, fallback)
    except Exception:
        return fallback


def _default_weather_fetcher(city: str, server_cfg: dict) -> str:
    target_city = str(city or "").strip()
    cached_reader = _lazy_proactive_attr("_read_cached_weather_hint", lambda _cfg: "")
    if not target_city:
        hint = cached_reader(server_cfg)
        if hint:
            return hint
    try:
        import nmc_weather_patch

        resolved_city = target_city
        if not resolved_city:
            plugins_cfg = server_cfg.get("plugins") if isinstance(server_cfg, dict) else {}
            if isinstance(plugins_cfg, dict):
                wcfg = plugins_cfg.get("get_weather") or {}
                if isinstance(wcfg, dict):
                    resolved_city = str(wcfg.get("default_location") or "").strip()
        resolved_city = resolved_city or "北京"
        resp = nmc_weather_patch.nmc_get_weather(None, location=resolved_city)
        text = str(getattr(resp, "result", "") or "").strip()
        if text:
            # Keep the first two informative lines (location + live condition) for concise speech
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return "；".join(lines[:2])[:140]
    except Exception:
        pass
    return cached_reader(server_cfg)


def _infer_character_name(server_cfg: dict) -> str:
    prompt_s = str((server_cfg or {}).get("prompt") or "")
    if "雷电芽衣" in prompt_s or "芽衣" in prompt_s:
        return "雷电芽衣"
    if "罗小黑" in prompt_s:
        return "罗小黑"
    return "小智"


def _default_qq_reminder_sender(title: str, body: str) -> Dict[str, Any]:
    try:
        import qq_bot_bridge

        return qq_bot_bridge.send_proactive_reminder(title, body)
    except ImportError:
        return {"ok": False, "reason": "qq_bot_unavailable", "error": "QQ 官方机器人模块尚未启用"}
    except Exception as exc:
        return {"ok": False, "reason": "qq_bot_error", "error": str(exc)}


# ---------------------------------------------------------------------------
# FutureTaskEngine (SQLite WAL + Background Scheduler)
# ---------------------------------------------------------------------------

class FutureTaskEngine:
    """Durable SQLite-backed scheduler for user-requested one-shot & cron future tasks."""

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        settings_path: Optional[str] = None,
        now_fn: Optional[Callable[[], float]] = None,
        local_dt_fn: Optional[Callable[[float], datetime]] = None,
        tts_synthesizer: Optional[Callable[[str, str, dict], Tuple[Optional[bytes], str]]] = None,
        speaker_player: Optional[Callable[[bytes, float], Tuple[bool, str]]] = None,
        busy_checker: Optional[Callable[[], bool]] = None,
        config_reader: Optional[Callable[[], dict]] = None,
        mobile_sender: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
        qq_sender: Optional[Callable[[str, str], Dict[str, Any]]] = None,
        weather_fetcher: Optional[Callable[[str, dict], str]] = None,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.db_path = os.path.abspath(db_path or DB_PATH)
        self.settings_path = settings_path
        self._now_fn = now_fn or time.time
        self._local_dt_fn = local_dt_fn or (lambda ts: datetime.fromtimestamp(ts).astimezone())
        self._tts_synthesizer = tts_synthesizer or self._default_tts_synth
        self._speaker_player = speaker_player or self._default_speaker_play
        self._busy_checker = busy_checker or self._default_busy_check
        self._config_reader = config_reader or self._default_config_read
        self._mobile_sender = mobile_sender
        self._qq_sender = qq_sender or _default_qq_reminder_sender
        self._weather_fetcher = weather_fetcher or _default_weather_fetcher
        self._poll_interval = max(1.0, float(poll_interval_seconds))

        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._schema_ready = False
        self._busy_defer_counts: Dict[str, int] = {}

    @staticmethod
    def _default_tts_synth(text: str, emotion: str, server_cfg: dict) -> Tuple[Optional[bytes], str]:
        fn = _lazy_proactive_attr("_default_synthesize_wav", None)
        if callable(fn):
            return fn(text, emotion, server_cfg)
        return None, "tts_unavailable"

    @staticmethod
    def _default_busy_check() -> bool:
        fn = _lazy_proactive_attr("_is_voice_pipeline_busy", None)
        return bool(fn()) if callable(fn) else False

    @staticmethod
    def _default_config_read() -> dict:
        fn = _lazy_proactive_attr("_read_server_yaml", None)
        return dict(fn() or {}) if callable(fn) else {}

    @staticmethod
    def _default_speaker_play(wav_bytes: bytes, vol: float) -> Tuple[bool, str]:
        import pc_audio_router

        return pc_audio_router.play_wav_on_pc_sync(wav_bytes, volume=vol)

    def _now(self) -> float:
        return float(self._now_fn())

    def _local_dt(self, ts: Optional[float] = None) -> datetime:
        return self._local_dt_fn(self._now() if ts is None else float(ts))

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema_locked(self) -> None:
        if self._schema_ready:
            return
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS future_tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    task_kind TEXT NOT NULL DEFAULT 'reminder',
                    schedule_type TEXT NOT NULL,
                    cron_expr TEXT NOT NULL DEFAULT '',
                    run_at REAL,
                    next_run_at REAL,
                    delivery TEXT NOT NULL DEFAULT 'pc_and_mobile',
                    respect_quiet_hours INTEGER NOT NULL DEFAULT 0,
                    city TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    run_count INTEGER NOT NULL DEFAULT 0,
                    last_run_at REAL,
                    last_result TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'console',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_future_tasks_due
                    ON future_tasks(status, next_run_at);

                CREATE TABLE IF NOT EXISTS future_task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    fired_at REAL NOT NULL,
                    delivery TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    rendered_text TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_future_task_runs_fired
                    ON future_task_runs(fired_at DESC);
                """
            )
            conn.commit()
            self._schema_ready = True
        finally:
            conn.close()

    def _row_to_task_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        nxt = row["next_run_at"]
        last_r = row["last_run_at"]
        return {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "message": str(row["message"]),
            "task_kind": str(row["task_kind"]),
            "schedule_type": str(row["schedule_type"]),
            "cron_expr": str(row["cron_expr"] or ""),
            "run_at": float(row["run_at"]) if row["run_at"] is not None else None,
            "next_run_at": float(nxt) if nxt is not None else None,
            "next_run_text": self._local_dt(float(nxt)).strftime("%Y-%m-%d %H:%M") if nxt is not None else "",
            "delivery": str(row["delivery"]),
            "respect_quiet_hours": bool(row["respect_quiet_hours"]),
            "city": str(row["city"] or ""),
            "status": str(row["status"]),
            "run_count": int(row["run_count"] or 0),
            "last_run_at": float(last_r) if last_r is not None else None,
            "last_run_text": self._local_dt(float(last_r)).strftime("%Y-%m-%d %H:%M") if last_r is not None else "",
            "last_result": str(row["last_result"] or ""),
            "source": str(row["source"] or "console"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _row_to_run_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        fired = float(row["fired_at"])
        return {
            "id": str(row["id"]),
            "task_id": str(row["task_id"]),
            "title": str(row["title"]),
            "fired_at": fired,
            "fired_text": self._local_dt(fired).strftime("%Y-%m-%d %H:%M:%S"),
            "delivery": str(row["delivery"]),
            "outcome": str(row["outcome"]),
            "detail": str(row["detail"] or ""),
            "rendered_text": str(row["rendered_text"] or ""),
        }

    def create_task(
        self,
        *,
        title: str,
        message: str = "",
        schedule_type: str = "once",
        run_at: Any = None,
        delay_minutes: Any = None,
        cron_expr: str = "",
        task_kind: str = "reminder",
        delivery: str = "pc_and_mobile",
        respect_quiet_hours: Optional[bool] = None,
        city: str = "",
        source: str = "console",
    ) -> Dict[str, Any]:
        clean_title = _sanitize_text(title, MAX_TITLE_CHARS)
        if not clean_title:
            raise ValueError("任务标题不能为空")
        clean_msg = _sanitize_text(message or clean_title, MAX_MESSAGE_CHARS)
        clean_city = _sanitize_text(city, 40)

        kind = str(task_kind or "reminder").strip().lower()
        if kind not in VALID_TASK_KINDS:
            raise ValueError(f"task_kind 仅支持 {', '.join(sorted(VALID_TASK_KINDS))}")

        stype = str(schedule_type or "").strip().lower()
        if not stype:
            stype = "cron" if str(cron_expr or "").strip() else "once"
        if stype not in VALID_SCHEDULE_TYPES:
            raise ValueError("schedule_type 仅支持 once 或 cron")

        deliv = str(delivery or "pc_and_mobile").strip().lower()
        if deliv not in VALID_DELIVERIES:
            raise ValueError(f"delivery 仅支持 {', '.join(sorted(VALID_DELIVERIES))}")

        now = self._now()
        now_dt = self._local_dt(now)
        norm_cron = ""
        run_at_ts: Optional[float] = None

        if stype == "cron":
            norm_cron = validate_cron_min_interval(
                str(cron_expr or "").strip(),
                min_minutes=MIN_CRON_INTERVAL_MINUTES,
                ref_dt=now_dt,
            )
            next_dt = next_cron_datetime(norm_cron, now_dt)
            next_ts = next_dt.timestamp()
        else:
            if delay_minutes is not None and str(delay_minutes).strip() != "":
                if isinstance(delay_minutes, bool):
                    raise ValueError("delay_minutes 必须为正数")
                try:
                    dm = float(delay_minutes)
                except (TypeError, ValueError) as exc:
                    raise ValueError("delay_minutes 必须为数字") from exc
                if dm <= 0 or dm > 60 * 24 * 365:
                    raise ValueError("delay_minutes 必须在 0 到 525600（1年）之间")
                target_dt = now_dt + timedelta(minutes=dm)
            else:
                target_dt = parse_run_at_input(run_at, now_dt=now_dt)
                if target_dt.timestamp() <= now - 5.0:
                    raise ValueError("一次性定时任务的触发时间必须晚于当前时间")
            run_at_ts = target_dt.timestamp()
            next_ts = run_at_ts

        if respect_quiet_hours is None:
            quiet_flag = 1 if (stype == "cron" and kind == "weather_briefing") else 0
        else:
            quiet_flag = 1 if bool(respect_quiet_hours) else 0

        src = str(source or "console").strip().lower()[:24] or "console"
        task_id = f"ft-{uuid.uuid4().hex[:10]}"

        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                active_cnt = conn.execute(
                    "SELECT COUNT(*) FROM future_tasks WHERE status IN ('active', 'paused')"
                ).fetchone()[0]
                if int(active_cnt) >= MAX_ACTIVE_TASKS:
                    raise ValueError(f"活跃定时任务已达上限（{MAX_ACTIVE_TASKS} 条），请先取消不需要的任务")
                conn.execute(
                    """
                    INSERT INTO future_tasks (
                        id, title, message, task_kind, schedule_type, cron_expr,
                        run_at, next_run_at, delivery, respect_quiet_hours, city,
                        status, run_count, last_run_at, last_result, source,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, NULL, '', ?, ?, ?)
                    """,
                    (
                        task_id,
                        clean_title,
                        clean_msg,
                        kind,
                        stype,
                        norm_cron,
                        run_at_ts,
                        next_ts,
                        deliv,
                        quiet_flag,
                        clean_city,
                        src,
                        now,
                        now,
                    ),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM future_tasks WHERE id = ?", (task_id,)).fetchone()
                task = self._row_to_task_dict(row)
            finally:
                conn.close()

        self._wake_event.set()
        return task

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        tid = str(task_id or "").strip()
        if not tid:
            return None
        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM future_tasks WHERE id = ?", (tid,)).fetchone()
                return self._row_to_task_dict(row) if row is not None else None
            finally:
                conn.close()

    def list_tasks(self, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                if include_inactive:
                    rows = conn.execute(
                        "SELECT * FROM future_tasks ORDER BY "
                        "CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, "
                        "COALESCE(next_run_at, 9999999999) ASC, updated_at DESC LIMIT 100"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM future_tasks WHERE status IN ('active', 'paused') "
                        "ORDER BY COALESCE(next_run_at, 9999999999) ASC, updated_at DESC"
                    ).fetchall()
                return [self._row_to_task_dict(r) for r in rows]
            finally:
                conn.close()

    def list_runs(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        lim = max(1, min(MAX_RUN_HISTORY, int(limit)))
        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM future_task_runs ORDER BY fired_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
                return [self._row_to_run_dict(r) for r in rows]
            finally:
                conn.close()

    def set_task_status(self, task_id: str, new_status: str) -> Dict[str, Any]:
        tid = str(task_id or "").strip()
        st = str(new_status or "").strip().lower()
        if st not in ("active", "paused", "cancelled"):
            raise ValueError("仅支持切换状态为 active / paused / cancelled")
        now = self._now()
        now_dt = self._local_dt(now)
        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM future_tasks WHERE id = ?", (tid,)).fetchone()
                if row is None:
                    raise KeyError(f"未找到定时任务: {tid}")
                next_run = row["next_run_at"]
                if st == "active":
                    if str(row["schedule_type"]) == "cron":
                        next_run = next_cron_datetime(str(row["cron_expr"]), now_dt).timestamp()
                    elif next_run is None or float(next_run) <= now:
                        raise ValueError("该一次性任务时间已过，无法恢复，请重新创建")
                elif st == "cancelled":
                    next_run = None
                conn.execute(
                    "UPDATE future_tasks SET status = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                    (st, next_run, now, tid),
                )
                conn.commit()
                updated = conn.execute("SELECT * FROM future_tasks WHERE id = ?", (tid,)).fetchone()
                task = self._row_to_task_dict(updated)
            finally:
                conn.close()
        self._busy_defer_counts.pop(tid, None)
        self._wake_event.set()
        return task

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        return self.set_task_status(task_id, "cancelled")

    def pause_task(self, task_id: str) -> Dict[str, Any]:
        return self.set_task_status(task_id, "paused")

    def resume_task(self, task_id: str) -> Dict[str, Any]:
        return self.set_task_status(task_id, "active")

    def delete_task(self, task_id: str) -> bool:
        tid = str(task_id or "").strip()
        if not tid:
            return False
        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM future_tasks WHERE id = ?", (tid,))
                conn.commit()
                deleted = cur.rowcount > 0
            finally:
                conn.close()
        self._busy_defer_counts.pop(tid, None)
        return deleted

    def find_task_by_keyword(self, keyword: str) -> Optional[Dict[str, Any]]:
        kw = str(keyword or "").strip().lower()
        if not kw:
            return None
        for t in self.list_tasks(include_inactive=False):
            if t["id"].lower() == kw or kw in t["title"].lower() or kw in t["message"].lower():
                return t
        return None

    def _record_run(
        self,
        *,
        task_id: str,
        title: str,
        fired_at: float,
        delivery: str,
        outcome: str,
        detail: str,
        rendered_text: str,
    ) -> Dict[str, Any]:
        run_id = f"ftr-{uuid.uuid4().hex[:10]}"
        safe_detail = _sanitize_text(detail, 240)
        safe_text = _sanitize_text(rendered_text, MAX_MESSAGE_CHARS)
        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO future_task_runs (
                        id, task_id, title, fired_at, delivery, outcome, detail, rendered_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, task_id, title, fired_at, delivery, outcome, safe_detail, safe_text),
                )
                conn.execute(
                    """
                    DELETE FROM future_task_runs
                    WHERE id NOT IN (
                        SELECT id FROM future_task_runs ORDER BY fired_at DESC LIMIT ?
                    )
                    """,
                    (MAX_RUN_HISTORY,),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM future_task_runs WHERE id = ?", (run_id,)).fetchone()
                return self._row_to_run_dict(row)
            finally:
                conn.close()

    def _compose_payload(self, task: Dict[str, Any], server_cfg: dict) -> str:
        msg = str(task.get("message") or task.get("title") or "").strip()
        if task.get("task_kind") == "weather_briefing":
            weather_str = ""
            try:
                weather_str = str(self._weather_fetcher(str(task.get("city") or ""), server_cfg) or "").strip()
            except Exception:
                weather_str = ""
            if weather_str:
                base = f"{msg}。{weather_str}" if msg else weather_str
                return _sanitize_text(base, MAX_MESSAGE_CHARS)
        return _sanitize_text(msg, MAX_MESSAGE_CHARS)

    def _send_mobile(self, title: str, body: str) -> Dict[str, Any]:
        if callable(self._mobile_sender):
            return dict(self._mobile_sender(title, body, "reminder") or {})
        import mobile_push

        return mobile_push.send_mobile_notification(title, body, category="reminder")

    def _deliver(
        self,
        task: Dict[str, Any],
        *,
        manual: bool = False,
        force_dry_run: bool = False,
    ) -> Tuple[str, str, str]:
        """Deliver one task; returns `(outcome, detail, rendered_text)`."""
        now = self._now()
        now_dt = self._local_dt(now)
        proactive_cfg = runtime_settings.get_proactive(self.settings_path)
        effective_delivery = "dry_run" if force_dry_run else str(task.get("delivery") or "pc_and_mobile")

        # Quiet hours check (only when task opted in and not manual trigger)
        if not manual and bool(task.get("respect_quiet_hours")):
            qs = str(proactive_cfg.get("quiet_start") or "23:00")
            qe = str(proactive_cfg.get("quiet_end") or "08:00")
            if _is_in_quiet_hours(now_dt, qs, qe):
                return "skipped_quiet_hours", f"处于免打扰时段 ({qs}-{qe})", ""

        server_cfg = self._config_reader() or {}
        rendered = self._compose_payload(task, server_cfg)
        cname = _infer_character_name(server_cfg)
        push_title = f"{cname} · 定时提醒（{task.get('title') or '提醒'}）"

        if effective_delivery == "dry_run":
            return "dry_run", "试运行记录（未外放/未推送）", rendered

        if effective_delivery == "mobile_push":
            res = self._send_mobile(push_title, rendered)
            if res.get("ok"):
                return "delivered_mobile", f"provider={res.get('provider') or 'mobile'}", rendered
            return "failed", str(res.get("error") or res.get("reason") or "mobile_push_failed"), rendered

        if effective_delivery == "qq_bot":
            qres = self._qq_sender(push_title, rendered)
            if isinstance(qres, dict) and qres.get("ok"):
                return "delivered_qq", "QQ 官方机器人已投递", rendered
            # Graceful fallback to mobile_push if enabled
            mres = self._send_mobile(push_title, rendered)
            if mres.get("ok"):
                qerr = (qres or {}).get("error") or (qres or {}).get("reason") or "qq_unavailable"
                return "delivered_mobile_fallback", f"QQ 不可用({qerr})已回退手机推送", rendered
            qerr = (qres or {}).get("error") or (qres or {}).get("reason") or "qq_failed"
            return "failed", f"qq_bot:{qerr}", rendered

        # pc_speaker / pc_and_mobile
        if not manual and self._busy_checker():
            return "deferred_busy", "语音通道正忙，稍后重试", rendered

        vol = float(proactive_cfg.get("pc_speaker_volume", 0.75))
        pc_ok = False
        pc_detail = ""
        try:
            wav_bytes, tts_src = self._tts_synthesizer(rendered, "gentle", server_cfg)
            if wav_bytes:
                play_ok, play_detail = self._speaker_player(wav_bytes, vol)
                pc_ok = bool(play_ok)
                pc_detail = f"tts={tts_src}" if pc_ok else f"play_failed:{play_detail}"
            else:
                pc_detail = f"tts_unavailable:{tts_src}"
        except Exception as exc:
            pc_detail = f"tts_error:{type(exc).__name__}"

        mobile_ok = False
        mobile_detail = ""
        if effective_delivery == "pc_and_mobile" or (not pc_ok and effective_delivery == "pc_speaker"):
            try:
                mres = self._send_mobile(push_title, rendered)
                mobile_ok = bool(mres.get("ok"))
                if mobile_ok:
                    mobile_detail = f"mobile={mres.get('provider') or 'ok'}"
                elif not mres.get("skipped"):
                    mobile_detail = f"mobile_err={mres.get('error') or mres.get('reason') or ''}"
            except Exception as exc:
                mobile_detail = f"mobile_err={type(exc).__name__}"

        if pc_ok and mobile_ok:
            return "delivered_pc_and_mobile", f"{pc_detail}; {mobile_detail}", rendered
        if pc_ok:
            return "delivered_pc", pc_detail, rendered
        if mobile_ok:
            return "delivered_mobile_fallback", f"{pc_detail}; {mobile_detail}", rendered
        return "failed", "; ".join(x for x in (pc_detail, mobile_detail) if x) or "delivery_failed", rendered

    def trigger_now(self, task_id: str, *, force_dry_run: bool = False) -> Dict[str, Any]:
        """Manually trigger a task (does not alter cron cadence or cancel active once tasks when dry_run)."""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"未找到定时任务: {task_id}")
        now = self._now()
        outcome, detail, rendered = self._deliver(task, manual=True, force_dry_run=force_dry_run)
        eff_deliv = "dry_run" if force_dry_run else str(task["delivery"])
        run_rec = self._record_run(
            task_id=task["id"],
            title=task["title"],
            fired_at=now,
            delivery=eff_deliv,
            outcome=outcome,
            detail=detail,
            rendered_text=rendered,
        )
        try:
            runtime_events.publish_event(
                "future_task_fired",
                task_id=task["id"],
                title=task["title"],
                outcome=outcome,
                delivery=eff_deliv,
                text=rendered,
                manual=True,
            )
        except Exception:
            pass
        return {"ok": outcome != "failed", "task": task, "run": run_rec}

    def tick(self) -> List[Dict[str, Any]]:
        """Process all currently due tasks once. Safe to call directly from unit tests."""
        if not self._tick_lock.acquire(blocking=False):
            return []
        results: List[Dict[str, Any]] = []
        try:
            now = self._now()
            now_dt = self._local_dt(now)

            with self._lock:
                self._ensure_schema_locked()
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT * FROM future_tasks WHERE status = 'active' "
                        "AND next_run_at IS NOT NULL AND next_run_at <= ? "
                        "ORDER BY next_run_at ASC LIMIT 16",
                        (now,),
                    ).fetchall()
                    due_tasks = [self._row_to_task_dict(r) for r in rows]
                finally:
                    conn.close()

            for task in due_tasks:
                tid = task["id"]
                scheduled_ts = float(task["next_run_at"] or now)
                overdue_sec = now - scheduled_ts

                # Restart storm protection: stale tasks older than STALE_GRACE_SECONDS
                if overdue_sec > STALE_GRACE_SECONDS:
                    if task["schedule_type"] == "once":
                        self._update_after_fire(
                            tid,
                            status="missed",
                            next_run_at=None,
                            increment_count=False,
                            last_run_at=now,
                            last_result=f"missed_stale({int(overdue_sec)}s)",
                        )
                        run_rec = self._record_run(
                            task_id=tid,
                            title=task["title"],
                            fired_at=now,
                            delivery=task["delivery"],
                            outcome="missed_stale",
                            detail=f"超过容忍窗口 {int(overdue_sec)} 秒未运行，已跳过防重启风暴",
                            rendered_text="",
                        )
                        results.append({"task_id": tid, "outcome": "missed_stale", "run": run_rec})
                    else:
                        nxt = next_cron_datetime(task["cron_expr"], now_dt).timestamp()
                        self._update_after_fire(
                            tid,
                            status="active",
                            next_run_at=nxt,
                            increment_count=False,
                            last_run_at=now,
                            last_result=f"advanced_stale({int(overdue_sec)}s)",
                        )
                        results.append({"task_id": tid, "outcome": "advanced_stale"})
                    self._busy_defer_counts.pop(tid, None)
                    continue

                outcome, detail, rendered = self._deliver(task, manual=False, force_dry_run=False)

                if outcome == "deferred_busy":
                    cnt = self._busy_defer_counts.get(tid, 0) + 1
                    if cnt <= MAX_BUSY_DEFERS:
                        self._busy_defer_counts[tid] = cnt
                        self._update_after_fire(
                            tid,
                            status="active",
                            next_run_at=now + BUSY_DEFER_SECONDS,
                            increment_count=False,
                            last_run_at=None,
                            last_result=f"deferred_busy({cnt}/{MAX_BUSY_DEFERS})",
                        )
                        results.append({"task_id": tid, "outcome": "deferred_busy"})
                        continue
                    # Exhausted busy retries -> try mobile push fallback cleanly
                    self._busy_defer_counts.pop(tid, None)
                    server_cfg = self._config_reader() or {}
                    push_title = f"{_infer_character_name(server_cfg)} · 定时提醒（{task['title']}）"
                    mres = self._send_mobile(push_title, rendered)
                    if mres.get("ok"):
                        outcome = "delivered_mobile_fallback"
                        detail = "语音通道持续占线，已自动改走手机推送"
                    else:
                        outcome = "skipped_busy"
                        detail = "语音通道持续占线，本轮跳过播报"
                else:
                    self._busy_defer_counts.pop(tid, None)

                # Advance schedule
                if task["schedule_type"] == "once":
                    next_status = "completed"
                    next_ts = None
                else:
                    next_status = "active"
                    next_ts = next_cron_datetime(task["cron_expr"], now_dt).timestamp()

                self._update_after_fire(
                    tid,
                    status=next_status,
                    next_run_at=next_ts,
                    increment_count=outcome.startswith("delivered") or outcome == "dry_run",
                    last_run_at=now,
                    last_result=f"{outcome}: {detail}"[:180],
                )
                run_rec = self._record_run(
                    task_id=tid,
                    title=task["title"],
                    fired_at=now,
                    delivery=task["delivery"],
                    outcome=outcome,
                    detail=detail,
                    rendered_text=rendered,
                )
                try:
                    runtime_events.publish_event(
                        "future_task_fired",
                        task_id=tid,
                        title=task["title"],
                        outcome=outcome,
                        delivery=task["delivery"],
                        text=rendered,
                        manual=False,
                    )
                except Exception:
                    pass
                results.append({"task_id": tid, "outcome": outcome, "run": run_rec})
        finally:
            self._tick_lock.release()
        return results

    def _update_after_fire(
        self,
        task_id: str,
        *,
        status: str,
        next_run_at: Optional[float],
        increment_count: bool,
        last_run_at: Optional[float],
        last_result: str,
    ) -> None:
        now = self._now()
        with self._lock:
            self._ensure_schema_locked()
            conn = self._connect()
            try:
                if last_run_at is not None:
                    conn.execute(
                        """
                        UPDATE future_tasks
                        SET status = ?,
                            next_run_at = ?,
                            run_count = run_count + ?,
                            last_run_at = ?,
                            last_result = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            next_run_at,
                            1 if increment_count else 0,
                            last_run_at,
                            _sanitize_text(last_result, 200),
                            now,
                            task_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE future_tasks
                        SET status = ?,
                            next_run_at = ?,
                            last_result = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            next_run_at,
                            _sanitize_text(last_result, 200),
                            now,
                            task_id,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_status(self, *, include_inactive: bool = False) -> Dict[str, Any]:
        tasks = self.list_tasks(include_inactive=include_inactive)
        active_cnt = sum(1 for t in tasks if t["status"] == "active")
        paused_cnt = sum(1 for t in tasks if t["status"] == "paused")
        runs = self.list_runs(limit=15)
        running = bool(self._thread is not None and self._thread.is_alive())
        return {
            "ok": True,
            "running": running,
            "active_count": active_cnt,
            "paused_count": paused_cnt,
            "max_active_tasks": MAX_ACTIVE_TASKS,
            "min_cron_interval_minutes": MIN_CRON_INTERVAL_MINUTES,
            "tasks": tasks,
            "runs": runs,
        }

    def start(self) -> None:
        with self._lock:
            self._ensure_schema_locked()
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            t = threading.Thread(target=self._loop, name="future-tasks-scheduler", daemon=True)
            self._thread = t
            t.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            t = self._thread
            self._stop_event.set()
            self._wake_event.set()
        if t is not None and t.is_alive():
            t.join(timeout=max(0.1, float(timeout)))

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"[FutureTasks] 调度循环异常: {exc}", flush=True)
            self._wake_event.wait(timeout=self._poll_interval)
            self._wake_event.clear()


# ---------------------------------------------------------------------------
# Singleton helpers (lazy initialization, zero import-time side effects)
# ---------------------------------------------------------------------------

_engine_lock = threading.Lock()
_engine: Optional[FutureTaskEngine] = None


def get_future_task_engine() -> FutureTaskEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = FutureTaskEngine()
        return _engine


def reset_future_task_engine(engine: Optional[FutureTaskEngine] = None) -> None:
    """Replace or clear singleton (for isolated unit tests)."""
    global _engine
    with _engine_lock:
        if _engine is not None and _engine is not engine:
            try:
                _engine.stop(timeout=0.5)
            except Exception:
                pass
        _engine = engine


def start_future_tasks() -> FutureTaskEngine:
    eng = get_future_task_engine()
    eng.start()
    return eng


def stop_future_tasks(timeout: float = 1.5) -> None:
    with _engine_lock:
        eng = _engine
    if eng is not None:
        eng.stop(timeout=timeout)
