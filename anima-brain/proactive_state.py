"""Proactive companion state for Anima Brain (Phase 1 ③).

Manages two lightweight side tables inside the authoritative `brain.sqlite3`:
1. `xiaozhi_open_loop_dispatch`: per-(owner, character_id, memory_id) ledger enforcing
   at-most-once proactive follow-up (`nudge_count >= 1` excluded), snooze/dismiss, and
   automatic expiry of stale open loops (event_date > 7d past, valid_until past, or
   dateless loops untouched for > 14d).
2. `xiaozhi_inner_life`: per-(owner, character_id, local_date, slot) micro-life slices
   (`morning` / `afternoon` / `evening`) grounded in character persona and optional cached
   weather, with cross-day deduplication and optional low-cost summary LLM generation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from companion_engine import emotion
from companion_engine.knowledge import eligible
from companion_engine.models import CharacterSpec

import recall_sanitizer

log = logging.getLogger("anima-proactive")

VALID_SLOTS = ("morning", "afternoon", "evening")
VALID_ACK_ACTIONS = frozenset({"nudged", "snooze", "dismiss", "reset"})
EVENT_EXPIRY_DAYS = 7
DATELESS_EXPIRY_DAYS = 14
DEFAULT_MIN_DATELESS_AGE_HOURS = 4.0

_WEATHER_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rain", ("雨", "阵雨", "雷阵雨", "小雨", "中雨", "大雨", "暴雨", "阴雨", "rain")),
    ("snow", ("雪", "小雪", "大雪", "暴雪", "雨夹雪", "snow")),
    ("cold", ("冷", "寒", "降温", "大风", "霜", "冻")),
    ("hot", ("热", "高温", "酷暑", "暴晒", "闷热")),
    ("sunny", ("晴", "阳光", "多云", "微风", "sunny")),
)

# Curated persona micro-life pools: (activity_tag, text, preferred_weather_or_empty)
_INNER_LIFE_POOLS: dict[str, dict[str, list[tuple[str, str, str]]]] = {
    "mei": {
        "morning": [
            (
                "mei_m_tea",
                "清晨刚泡了一壶清淡的茉莉绿茶，窗台边还有些微凉的晨风。",
                "",
            ),
            (
                "mei_m_breakfast",
                "早上试着做了软嫩的厚蛋烧，厨房里还留着一点暖烘烘的香气。",
                "",
            ),
            (
                "mei_m_rain_window",
                "上午听着窗外的雨声把书桌擦拭了一遍，顺手温了杯热茶。",
                "rain",
            ),
            (
                "mei_m_sun_plants",
                "趁上午阳光不错，给窗边的小盆栽浇了水，叶片显得格外精神。",
                "sunny",
            ),
            (
                "mei_m_recipe_notes",
                "上午翻了翻手写的料理笔记，在琢磨晚上要不要试一道清爽的小菜。",
                "",
            ),
            (
                "mei_m_warm_ginger",
                "天微凉，早上煮了点暖胃的姜枣茶，屋里安静又舒服。",
                "cold",
            ),
        ],
        "afternoon": [
            (
                "mei_a_reading",
                "午后靠在窗边翻了几页散文，光影慢悠悠地挪到了书页一角。",
                "",
            ),
            (
                "mei_a_baking",
                "下午试着烤了几块微甜的黄油小饼干，火候刚好。",
                "",
            ),
            (
                "mei_a_rain_tea",
                "午后外面雨意正浓，泡了壶红茶配着滴答雨声安静待了一会儿。",
                "rain",
            ),
            (
                "mei_a_tidy_desk",
                "下午顺手整理了杂物架，把几件常用小物摆得整整齐齐。",
                "",
            ),
            (
                "mei_a_iced_tea",
                "天气偏热，下午提前冷泡了一壶乌龙茶，喝一口清爽不少。",
                "hot",
            ),
            (
                "mei_a_music",
                "午后放了一段舒缓的钢琴曲，趁空闲让思绪放空了一小会儿。",
                "",
            ),
        ],
        "evening": [
            (
                "mei_e_soup",
                "傍晚慢慢炖了一锅清甜的萝卜排骨汤，热气让屋里暖和起来。",
                "",
            ),
            (
                "mei_e_lamp",
                "天色暗下来后点亮了暖黄的台灯，泡了杯安神花茶静静坐着。",
                "",
            ),
            (
                "mei_e_rain_night",
                "夜里窗外飘着细雨，把窗帘拉好后屋里显得格外安稳。",
                "rain",
            ),
            (
                "mei_e_stroll_sky",
                "傍晚抬头看了会儿渐暗的晚霞，空气比白天柔和了许多。",
                "sunny",
            ),
            (
                "mei_e_warm_milk",
                "入夜有点转凉，温了一小杯热牛奶，顺便把明天的清单理顺了。",
                "cold",
            ),
            (
                "mei_e_diary",
                "晚上翻看今天的琐碎记录，觉得平静的日子也很值得珍惜。",
                "",
            ),
        ],
    },
    "luoxiaohei": {
        "morning": [
            (
                "lxh_m_sunbeam",
                "早上在窗台那一小块太阳底下伸了个大懒腰，晒得毛茸茸的。",
                "sunny",
            ),
            (
                "lxh_m_breakfast",
                "一大早就闻到巷口早点摊的香味，认真思考了三秒要不要加个烤肠。",
                "",
            ),
            (
                "lxh_m_rain_watch",
                "上午趴在窗边看雨点在玻璃上赛跑，顺便数了数路过几把伞。",
                "rain",
            ),
            (
                "lxh_m_bug",
                "刚才盯着桌角一只迷路的小甲虫看了半天，最后把它轻轻送回窗外了。",
                "",
            ),
            (
                "lxh_m_curled",
                "早上风有点凉，缩成一团多赖了五分钟才精神抖擞爬起来。",
                "cold",
            ),
        ],
        "afternoon": [
            (
                "lxh_a_nap",
                "午后阳光刚好，差点就在椅子上打了个盹，好在及时醒神了。",
                "",
            ),
            (
                "lxh_a_snacks",
                "下午翻了翻零食柜，发现小鱼干存货告急，得记在心上。",
                "",
            ),
            (
                "lxh_a_clouds",
                "下午盯着窗外飘过的云看了一会儿，有一朵特别像刚出炉的包子。",
                "sunny",
            ),
            (
                "lxh_a_rain_nap",
                "外面噼里啪啦下雨，待在屋里听雨声觉得特别适合发呆。",
                "rain",
            ),
            (
                "lxh_a_cool_floor",
                "天气有点热，找了块最凉快的角落趴着吹了会儿风。",
                "hot",
            ),
        ],
        "evening": [
            (
                "lxh_e_bbq_thought",
                "傍晚空气里隐约飘来烤肉味，肚子立刻很有默契地抗议了一声。",
                "",
            ),
            (
                "lxh_e_rooftop_breeze",
                "天擦黑时吹了会儿晚风，街上的灯一盏盏亮起来还挺好看的。",
                "",
            ),
            (
                "lxh_e_rain_cozy",
                "晚上外面湿漉漉的，窝在亮堂堂的屋里感觉特别安心。",
                "rain",
            ),
            (
                "lxh_e_stars",
                "夜里顺手抬头看了眼窗外夜空，安静得连风声都听得清清楚楚。",
                "",
            ),
            (
                "lxh_e_tidy",
                "晚上把白天的零碎物件归了位，今天也算靠谱收尾啦。",
                "",
            ),
        ],
    },
    "default": {
        "morning": [
            (
                "def_m_tea",
                "早上泡了杯温热清茶，趁安静整理了一下今天的待办节奏。",
                "",
            ),
            (
                "def_m_sun",
                "上午光线透过窗边洒进来，顺手把桌面收拾得清清爽爽。",
                "sunny",
            ),
            (
                "def_m_rain",
                "上午窗外飘着雨，听着淅沥水声安静待在桌边。",
                "rain",
            ),
            (
                "def_m_news",
                "清晨翻了翻几条轻松的科技和生活趣闻，心情挺明朗的。",
                "",
            ),
            (
                "def_m_warm",
                "早上空气微凉，捧着温水慢慢醒神，状态已经上线啦。",
                "cold",
            ),
        ],
        "afternoon": [
            (
                "def_a_music",
                "午后放了首轻快的纯音乐，趁空档舒展了一下节奏。",
                "",
            ),
            (
                "def_a_clouds",
                "下午看了眼窗外慢慢飘过的云，阳光不算刺眼，挺惬意。",
                "sunny",
            ),
            (
                "def_a_rain",
                "午后雨声细细密密的，屋里亮着柔和灯光显得很安静。",
                "rain",
            ),
            (
                "def_a_iced",
                "下午有点闷热，给自己备了杯清凉冰饮提提神。",
                "hot",
            ),
            (
                "def_a_notes",
                "下午顺手翻了翻近期的零散笔记，把几个好玩的小点子理顺了。",
                "",
            ),
        ],
        "evening": [
            (
                "def_e_sunset",
                "傍晚窗外天色慢慢暗下来，暖灯一开，整个房间都松弛了不少。",
                "",
            ),
            (
                "def_e_rain",
                "晚上外面落着小雨，待在温暖的桌边静静守着特别踏实。",
                "rain",
            ),
            (
                "def_e_chill",
                "入夜后节奏慢了下来，放了段轻音乐歇了歇脚。",
                "",
            ),
            (
                "def_e_warm_drink",
                "晚风偏凉，热了杯暖饮慢悠悠喝着，准备迎接清净的夜晚。",
                "cold",
            ),
            (
                "def_e_review",
                "晚上把今天的琐事复盘了一遍，觉得平淡顺遂也挺好。",
                "",
            ),
        ],
    },
}


def ensure_proactive_tables(engine) -> None:
    """Idempotently create Phase 1 open-loop dispatch & inner-life tables in `brain.sqlite3`."""
    with engine._transaction():
        engine.db.execute(
            "CREATE TABLE IF NOT EXISTS xiaozhi_open_loop_dispatch ("
            "owner TEXT NOT NULL, "
            "character_id TEXT NOT NULL, "
            "memory_id TEXT NOT NULL, "
            "nudge_count INTEGER NOT NULL DEFAULT 0, "
            "last_nudged_at REAL NOT NULL DEFAULT 0, "
            "snoozed_until REAL NOT NULL DEFAULT 0, "
            "auto_expired_at REAL NOT NULL DEFAULT 0, "
            "resolved_reason TEXT NOT NULL DEFAULT '', "
            "last_event_id TEXT NOT NULL DEFAULT '', "
            "updated_at REAL NOT NULL DEFAULT 0, "
            "PRIMARY KEY(owner, character_id, memory_id))"
        )
        cols = {
            str(r[1])
            for r in engine.db.execute(
                "PRAGMA table_info(xiaozhi_open_loop_dispatch)"
            ).fetchall()
        }
        if "last_event_id" not in cols:
            engine.db.execute(
                "ALTER TABLE xiaozhi_open_loop_dispatch "
                "ADD COLUMN last_event_id TEXT NOT NULL DEFAULT ''"
            )
        engine.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_open_loop_dispatch_scope "
            "ON xiaozhi_open_loop_dispatch(owner, character_id, auto_expired_at)"
        )
        engine.db.execute(
            "CREATE TABLE IF NOT EXISTS xiaozhi_inner_life ("
            "owner TEXT NOT NULL, "
            "character_id TEXT NOT NULL, "
            "local_date TEXT NOT NULL, "
            "slot TEXT NOT NULL, "
            "text TEXT NOT NULL, "
            "activity_tag TEXT NOT NULL DEFAULT '', "
            "weather_hint TEXT NOT NULL DEFAULT '', "
            "source TEXT NOT NULL DEFAULT 'template', "
            "created_at REAL NOT NULL DEFAULT 0, "
            "updated_at REAL NOT NULL DEFAULT 0, "
            "PRIMARY KEY(owner, character_id, local_date, slot))"
        )
        engine.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_inner_life_recent "
            "ON xiaozhi_inner_life(owner, character_id, local_date DESC)"
        )


def _resolve_tz(tz_name: str | None) -> ZoneInfo:
    raw = str(tz_name or "").strip() or "Asia/Shanghai"
    if raw.upper() == "UTC":
        # Default characters created without explicit timezone still follow Asia/Shanghai in XiaoZhi
        return ZoneInfo("Asia/Shanghai")
    try:
        return ZoneInfo(raw)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def resolve_local_date_and_slot(
    tz_name: str | None = "Asia/Shanghai",
    *,
    now: float | None = None,
) -> tuple[str, str, date]:
    """Return `(local_date_iso, slot, local_date_obj)` for the timezone and epoch timestamp."""
    ts = float(now) if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, _resolve_tz(tz_name))
    hour = dt.hour
    if 5 <= hour < 12:
        slot = "morning"
    elif 12 <= hour < 18:
        slot = "afternoon"
    else:
        slot = "evening"
    d = dt.date()
    return d.isoformat(), slot, d


def _classify_weather_tag(weather_hint: str) -> str:
    txt = str(weather_hint or "").strip().lower()
    if not txt:
        return ""
    for tag, kws in _WEATHER_KEYWORDS:
        if any(kw in txt for kw in kws):
            return tag
    return ""


def _detect_persona_archetype(spec: CharacterSpec) -> str:
    blob = f"{spec.name or ''} {spec.persona or ''}"
    if "雷电芽衣" in blob or "芽衣" in blob:
        return "mei"
    if "罗小黑" in blob or "小黑妖精" in blob:
        return "luoxiaohei"
    return "default"


def _memory_last_touched_ts(memory: Any) -> float:
    lc = getattr(memory, "lifecycle", None)
    reinf = float(getattr(lc, "last_reinforced_at", None) or 0.0)
    obs = float(getattr(lc, "observed_at", None) or 0.0)
    upd = float(getattr(memory, "updated_at", None) or 0.0)
    return max(reinf, obs, upd)


def is_open_loop_expired(
    memory: Any,
    *,
    today: date,
    now: float,
    dispatch_row: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Determine if an open loop should be treated as expired/inactive."""
    row = dispatch_row if isinstance(dispatch_row, dict) else {}
    reason = str(row.get("resolved_reason") or "").strip()
    if reason in ("dismissed", "resolved"):
        return True, reason

    details = getattr(memory, "details", None)
    lifecycle = getattr(memory, "lifecycle", None)
    event_end = getattr(details, "event_end", None)
    event_date = getattr(details, "event_date", None)
    ref_date = event_end or event_date
    if isinstance(ref_date, date):
        if (today - ref_date).days > EVENT_EXPIRY_DAYS:
            return True, "event_date_expired"

    valid_until = getattr(lifecycle, "valid_until", None)
    if isinstance(valid_until, date) and today > valid_until:
        return True, "valid_until_expired"

    last_touched = _memory_last_touched_ts(memory)
    auto_exp = float(row.get("auto_expired_at") or 0.0)
    if auto_exp > 0.0:
        # Allow revival ONLY when a dateless stale loop was genuinely reinforced after auto-expiry
        revived = (
            reason == "stale_14d"
            and ref_date is None
            and last_touched > (auto_exp + 1.0)
            and (now - last_touched) <= (DATELESS_EXPIRY_DAYS * 86400.0)
        )
        if not revived:
            return True, reason or "auto_expired"

    if ref_date is None and last_touched > 0.0:
        if (now - last_touched) > (DATELESS_EXPIRY_DAYS * 86400.0):
            return True, "stale_14d"

    return False, ""


def _load_dispatch_map_locked(
    engine, character_id: str, memory_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not memory_ids:
        return {}
    placeholders = ",".join("?" for _ in memory_ids)
    try:
        rows = engine.db.execute(
            f"SELECT memory_id, nudge_count, last_nudged_at, snoozed_until, "
            f"auto_expired_at, resolved_reason, last_event_id, updated_at "
            f"FROM xiaozhi_open_loop_dispatch "
            f"WHERE owner=? AND character_id=? AND memory_id IN ({placeholders})",
            (engine.owner, character_id, *memory_ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(r["memory_id"]): dict(r) for r in rows}


def filter_active_open_loops_readonly(
    engine,
    character_id: str,
    loops: list[Any],
    *,
    today: date,
    now: float,
) -> list[Any]:
    """Fast read-only filter for `build_fast_recall_context` (caller holds `engine._lock`)."""
    if not loops:
        return []
    dispatch_map = _load_dispatch_map_locked(engine, character_id, [str(m.id) for m in loops])
    active: list[Any] = []
    for m in loops:
        expired, _ = is_open_loop_expired(
            m,
            today=today,
            now=now,
            dispatch_row=dispatch_map.get(str(m.id)),
        )
        if not expired:
            active.append(m)
    return active


def _sweep_and_load_dispatch(
    engine,
    character_id: str,
    loops: list[Any],
    *,
    today: date,
    now: float,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Persist auto-expiry marks for stale open loops and return `(dispatch_map, expired_count)`."""
    ensure_proactive_tables(engine)
    with engine._lock:
        dispatch_map = _load_dispatch_map_locked(engine, character_id, [str(m.id) for m in loops])

    newly_expired: list[tuple[str, str]] = []
    expired_total = 0
    for m in loops:
        mid = str(m.id)
        row = dispatch_map.get(mid)
        expired, reason = is_open_loop_expired(m, today=today, now=now, dispatch_row=row)
        if expired:
            expired_total += 1
            if not row or float(row.get("auto_expired_at") or 0.0) <= 0.0:
                newly_expired.append((mid, reason))

    if newly_expired:
        with engine._transaction():
            for mid, reason in newly_expired:
                engine.db.execute(
                    "INSERT INTO xiaozhi_open_loop_dispatch("
                    "owner, character_id, memory_id, nudge_count, last_nudged_at, "
                    "snoozed_until, auto_expired_at, resolved_reason, last_event_id, updated_at"
                    ") VALUES(?, ?, ?, 0, 0, 0, ?, ?, '', ?) "
                    "ON CONFLICT(owner, character_id, memory_id) DO UPDATE SET "
                    "auto_expired_at=CASE WHEN auto_expired_at<=0 THEN excluded.auto_expired_at "
                    "ELSE auto_expired_at END, "
                    "resolved_reason=CASE WHEN resolved_reason='' THEN excluded.resolved_reason "
                    "ELSE resolved_reason END, "
                    "updated_at=excluded.updated_at",
                    (engine.owner, character_id, mid, now, reason, now),
                )
        with engine._lock:
            dispatch_map = _load_dispatch_map_locked(
                engine, character_id, [str(m.id) for m in loops]
            )

    return dispatch_map, expired_total


def _sanitize_short_text(text: str, max_len: int = 120) -> str:
    clean = " ".join(str(text or "").strip().split())
    clean, _ = recall_sanitizer.redact_sensitive_entities(clean)
    if len(clean) > max_len:
        clean = clean[: max_len - 1].rstrip() + "…"
    return clean


_LOCAL_LLM_HOSTS = frozenset({"host.docker.internal", "127.0.0.1", "localhost", "::1"})


def _is_local_endpoint(endpoint: str) -> bool:
    """Return True only when endpoint's parsed hostname matches an explicit local/loopback host."""
    try:
        parsed = urlparse(str(endpoint or "").strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return False
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return False
    return host in _LOCAL_LLM_HOSTS


def _try_generate_inner_life_llm(
    brain,
    spec: CharacterSpec,
    *,
    local_date: str,
    slot: str,
    weather_hint: str,
    recent_texts: list[str],
) -> str | None:
    """Optional low-cost summary LLM path for Inner Life generation (strict fail-soft)."""
    settings = getattr(getattr(brain, "runtime", None), "settings", None)
    if settings is None:
        return None
    base_url = (
        getattr(settings, "summary_base_url", None)
        or getattr(settings, "base_url", None)
        or ""
    ).strip()
    model = (
        getattr(settings, "summary_model", None)
        or getattr(settings, "model", None)
        or ""
    ).strip()
    key_obj = getattr(settings, "summary_key", None) or getattr(settings, "api_key", None)
    api_key = ""
    if key_obj is not None:
        api_key = (
            key_obj.get_secret_value()
            if hasattr(key_obj, "get_secret_value")
            else str(key_obj)
        ).strip()
    url = base_url.rstrip("/")
    if url and not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    is_local = _is_local_endpoint(url)
    if not url or not model or (not api_key and not is_local):
        return None

    slot_zh = {"morning": "上午", "afternoon": "下午", "evening": "傍晚/夜间"}.get(slot, "此刻")
    avoid_line = ""
    if recent_texts:
        avoid_line = "避免与最近几天雷同：" + " / ".join(t[:30] for t in recent_texts[:4])
    weather_line = f"当地天气参考：{weather_hint[:60]}" if weather_hint else ""
    prompt = (
        f"你是{spec.name}。人设摘要：{(spec.persona or '')[:240]}\n"
        f"现在是 {local_date} 的{slot_zh}。{weather_line}\n"
        f"{avoid_line}\n"
        "请用第一人称或简短旁白写 1 句角色此刻正在做的生活微日常（18~48字，温柔自然、"
        "不提及用户、不带引号、不带 Markdown、不输出解释）。"
    )
    try:
        import httpx

        req_headers = {"Content-Type": "application/json"}
        if api_key:
            req_headers["Authorization"] = f"Bearer {api_key}"
        req_json = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 90,
            "temperature": 0.85,
            "stream": False,
        }
        try:
            with httpx.Client(
                timeout=6.0, trust_env=not is_local, follow_redirects=False
            ) as client:
                resp = client.post(url, headers=req_headers, json=req_json)
        except ImportError:
            with httpx.Client(timeout=6.0, trust_env=False, follow_redirects=False) as client:
                resp = client.post(url, headers=req_headers, json=req_json)
        if resp.status_code != 200:
            return None
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        content = str(((choices[0] or {}).get("message") or {}).get("content") or "").strip()
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"[*`#>\"'「」『』\[\]]+", "", content).strip()
        content = " ".join(ln.strip() for ln in content.splitlines() if ln.strip())
        if len(content) < 8:
            return None
        return _sanitize_short_text(content, max_len=96)
    except Exception as exc:
        log.debug(
            "Inner life LLM generation skipped (falling back to template): %s",
            type(exc).__name__,
        )
        return None


def _pick_template_inner_life(
    spec: CharacterSpec,
    *,
    character_id: str,
    local_date: str,
    slot: str,
    weather_hint: str,
    recent_tags: set[str],
    recent_texts: set[str],
    salt: int = 0,
    exclude_current: tuple[str, str] | None = None,
) -> tuple[str, str]:
    archetype = _detect_persona_archetype(spec)
    pool = list((_INNER_LIFE_POOLS.get(archetype) or _INNER_LIFE_POOLS["default"]).get(slot) or [])
    if not pool:
        pool = list(_INNER_LIFE_POOLS["default"]["evening"])

    weather_tag = _classify_weather_tag(weather_hint)

    # Prefer unused items not seen in recent 3 days
    fresh = [
        item for item in pool if item[0] not in recent_tags and item[1] not in recent_texts
    ]
    if fresh:
        candidates = fresh
    elif exclude_current is not None:
        cur_tag, cur_text = exclude_current
        non_cur = [item for item in pool if item[0] != cur_tag and item[1] != cur_text]
        candidates = non_cur or pool
    else:
        candidates = pool

    if weather_tag:
        weather_matched = [item for item in candidates if item[2] == weather_tag]
        if weather_matched:
            candidates = weather_matched
        else:
            # Exclude contradictory weather slices (e.g., sunny slice on a rainy day)
            neutral = [item for item in candidates if not item[2]]
            if neutral:
                candidates = neutral
    else:
        neutral = [item for item in candidates if not item[2]]
        if neutral:
            candidates = neutral

    seed = f"{character_id}:{local_date}:{slot}:{salt}".encode("utf-8")
    idx = int(hashlib.sha256(seed).hexdigest()[:8], 16) % len(candidates)
    chosen_tag, chosen_text, _ = candidates[idx]
    return chosen_tag, chosen_text


def get_active_inner_life_hint(
    brain,
    character_id: str,
    *,
    now: float | None = None,
    locked: bool = False,
) -> dict[str, Any] | None:
    """Fast read-only lookup of today's active inner-life slice (safe inside fast recall)."""
    engine = brain.engine

    def _query() -> dict[str, Any] | None:
        try:
            row = engine._character(character_id)
        except Exception:
            return None
        spec = CharacterSpec.model_validate_json(row["spec"])
        local_date_str, slot, _ = resolve_local_date_and_slot(spec.timezone, now=now)
        try:
            hit = engine.db.execute(
                "SELECT local_date, slot, text, activity_tag, weather_hint, source, updated_at "
                "FROM xiaozhi_inner_life "
                "WHERE owner=? AND character_id=? AND local_date=? AND slot=?",
                (engine.owner, character_id, local_date_str, slot),
            ).fetchone()
            if hit is None:
                hit = engine.db.execute(
                    "SELECT local_date, slot, text, activity_tag, weather_hint, source, updated_at "
                    "FROM xiaozhi_inner_life "
                    "WHERE owner=? AND character_id=? AND local_date=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (engine.owner, character_id, local_date_str),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        if hit is None:
            return None
        rec = dict(hit)
        rec["text"] = _sanitize_short_text(str(rec.get("text") or ""), max_len=120)
        if not rec["text"]:
            return None
        return rec

    if locked:
        return _query()
    with engine._lock:
        return _query()


def get_or_create_inner_life(
    brain,
    character_id: str,
    *,
    weather_hint: str = "",
    slot: str | None = None,
    now: float | None = None,
    force_refresh: bool = False,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Return today's character Inner Life slice, generating one if absent or forced."""
    engine = brain.engine
    ensure_proactive_tables(engine)
    ts = float(now) if now is not None else time.time()
    clean_weather = _sanitize_short_text(weather_hint, max_len=80)

    with engine._lock:
        row = engine._character(character_id)
        spec = CharacterSpec.model_validate_json(row["spec"])
        local_date_str, auto_slot, _ = resolve_local_date_and_slot(spec.timezone, now=ts)
        target_slot = str(slot or "").strip().lower()
        if target_slot not in VALID_SLOTS:
            target_slot = auto_slot

        existing = engine.db.execute(
            "SELECT local_date, slot, text, activity_tag, weather_hint, "
            "source, created_at, updated_at "
            "FROM xiaozhi_inner_life "
            "WHERE owner=? AND character_id=? AND local_date=? AND slot=?",
            (engine.owner, character_id, local_date_str, target_slot),
        ).fetchone()
        if existing is not None and not force_refresh:
            rec = dict(existing)
            rec["text"] = _sanitize_short_text(str(rec.get("text") or ""), max_len=120)
            rec["character_id"] = character_id
            rec["character_name"] = spec.name
            rec["refreshed"] = False
            return rec

        recent_rows = engine.db.execute(
            "SELECT local_date, slot, text, activity_tag "
            "FROM xiaozhi_inner_life "
            "WHERE owner=? AND character_id=? "
            "ORDER BY local_date DESC, updated_at DESC LIMIT 9",
            (engine.owner, character_id),
        ).fetchall()

    recent_tags = {str(r["activity_tag"] or "") for r in recent_rows if r["activity_tag"]}
    recent_texts_list = [str(r["text"] or "") for r in recent_rows if r["text"]]
    recent_texts_set = set(recent_texts_list)
    if existing is not None:
        if existing["activity_tag"]:
            recent_tags.add(str(existing["activity_tag"]))
        if existing["text"]:
            recent_texts_set.add(str(existing["text"]))

    chosen_text = ""
    chosen_tag = ""
    source = "template"
    if use_llm:
        llm_text = _try_generate_inner_life_llm(
            brain,
            spec,
            local_date=local_date_str,
            slot=target_slot,
            weather_hint=clean_weather,
            recent_texts=recent_texts_list,
        )
        if llm_text and llm_text not in recent_texts_set:
            chosen_text = llm_text
            chosen_tag = f"llm_{target_slot}_{int(ts)}"
            source = "llm"

    if not chosen_text:
        salt = int(ts) if force_refresh else 0
        exclude_cur = (
            (str(existing["activity_tag"] or ""), str(existing["text"] or ""))
            if (force_refresh and existing is not None)
            else None
        )
        chosen_tag, chosen_text = _pick_template_inner_life(
            spec,
            character_id=character_id,
            local_date=local_date_str,
            slot=target_slot,
            weather_hint=clean_weather,
            recent_tags=recent_tags,
            recent_texts=recent_texts_set,
            salt=salt,
            exclude_current=exclude_cur,
        )
        source = "template"

    chosen_text = _sanitize_short_text(chosen_text, max_len=120)
    with engine._transaction():
        engine.db.execute(
            "INSERT INTO xiaozhi_inner_life("
            "owner, character_id, local_date, slot, text, activity_tag, weather_hint, "
            "source, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner, character_id, local_date, slot) DO UPDATE SET "
            "text=excluded.text, activity_tag=excluded.activity_tag, "
            "weather_hint=excluded.weather_hint, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (
                engine.owner,
                character_id,
                local_date_str,
                target_slot,
                chosen_text,
                chosen_tag,
                clean_weather,
                source,
                ts,
                ts,
            ),
        )

    return {
        "character_id": character_id,
        "character_name": spec.name,
        "local_date": local_date_str,
        "slot": target_slot,
        "text": chosen_text,
        "activity_tag": chosen_tag,
        "weather_hint": clean_weather,
        "source": source,
        "updated_at": ts,
        "refreshed": True,
    }


def refresh_inner_life(
    brain,
    character_id: str,
    *,
    weather_hint: str = "",
    slot: str | None = None,
    now: float | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Force-refresh today's character Inner Life slice while avoiding recent duplicates."""
    return get_or_create_inner_life(
        brain,
        character_id,
        weather_hint=weather_hint,
        slot=slot,
        now=now,
        force_refresh=True,
        use_llm=use_llm,
    )


def get_proactive_context(
    brain,
    character_id: str,
    *,
    weather_hint: str = "",
    now: float | None = None,
    ensure_inner_life: bool = True,
    min_dateless_age_hours: float = DEFAULT_MIN_DATELESS_AGE_HOURS,
) -> dict[str, Any]:
    """Assemble proactive companion context: recency, mood, <=2 due open loops, and inner life."""
    engine = brain.engine
    ensure_proactive_tables(engine)
    ts = float(now) if now is not None else time.time()

    with engine._lock:
        row = engine._character(character_id)
        spec = CharacterSpec.model_validate_json(row["spec"])
        local_date_str, slot, today = resolve_local_date_and_slot(spec.timezone, now=ts)

        # 1. Last committed turn timestamp
        last_turn_row = engine.db.execute(
            "SELECT MAX(created_at) AS last_ts FROM turns "
            "WHERE owner=? AND character_id=? AND status='committed'",
            (engine.owner, character_id),
        ).fetchone()
        last_turn_at = float(last_turn_row["last_ts"] or 0.0) if last_turn_row else 0.0
        hours_since_last_chat = (
            round(max(0.0, ts - last_turn_at) / 3600.0, 2) if last_turn_at > 0.0 else None
        )

        # 2. Decayed emotional / relationship summary (read-only)
        mood_info: dict[str, Any] = {
            "stage": "familiar",
            "last_emotion": "calm",
            "romance_enabled": bool(spec.romance_enabled),
            "warmth": 0.5,
            "familiarity": 0.5,
        }
        if spec.relationship_enabled:
            state = emotion.decayed(
                json.loads(row["state"]), ts, f"{engine.owner}:{character_id}"
            )
            bands = state.get("bands") or {}
            axes = state.get("axes") or {}
            fam_band = max(0, min(3, int(bands.get("familiarity", 0))))
            stage_label = ("stranger", "acquaintance", "familiar", "close")[fam_band]
            mood_info = {
                "stage": stage_label,
                "last_emotion": str(state.get("last_emotion") or "calm"),
                "romance_enabled": bool(spec.romance_enabled),
                "warmth": round(float(axes.get("warmth", 0.5)), 2),
                "familiarity": round(float(axes.get("familiarity", 0.5)), 2),
            }

        # 3. Candidate open loops
        raw_loops = [m for m in engine.memory.loops(character_id) if eligible(m, today=today)]

    dispatch_map, expired_count = _sweep_and_load_dispatch(
        engine, character_id, raw_loops, today=today, now=ts
    )

    min_age_sec = max(0.0, float(min_dateless_age_hours)) * 3600.0
    due_loops: list[dict[str, Any]] = []
    for m in raw_loops:
        mid = str(m.id)
        drow = dispatch_map.get(mid) or {}
        expired, _ = is_open_loop_expired(m, today=today, now=ts, dispatch_row=drow)
        if expired:
            continue
        if int(drow.get("nudge_count") or 0) >= 1:
            continue
        if float(drow.get("snoozed_until") or 0.0) > ts:
            continue

        ev_date = getattr(m.details, "event_date", None)
        if isinstance(ev_date, date):
            days_until = (ev_date - today).days
            # Due when event is tomorrow, today, or passed within EVENT_EXPIRY_DAYS
            if days_until > 1:
                continue
        else:
            last_touched = _memory_last_touched_ts(m)
            if last_touched > 0.0 and (ts - last_touched) < min_age_sec:
                continue

        safe_text = _sanitize_short_text(str(m.text or ""), max_len=140)
        if not safe_text:
            continue
        due_loops.append(
            {
                "memory_id": mid,
                "key": str(m.key or ""),
                "text": safe_text,
                "event_date": ev_date.isoformat() if isinstance(ev_date, date) else "",
                "importance": int(getattr(m.details, "importance", 5) or 5),
                "updated_at": float(getattr(m, "updated_at", 0.0) or 0.0),
            }
        )
        if len(due_loops) >= 2:
            break

    if ensure_inner_life:
        inner_life = get_or_create_inner_life(
            brain,
            character_id,
            weather_hint=weather_hint,
            now=ts,
            force_refresh=False,
            use_llm=False,
        )
    else:
        inner_life = get_active_inner_life_hint(brain, character_id, now=ts) or {}

    return {
        "ok": True,
        "character_id": character_id,
        "character_name": spec.name,
        "local_date": local_date_str,
        "slot": slot,
        "now": ts,
        "last_turn_at": last_turn_at,
        "hours_since_last_chat": hours_since_last_chat,
        "mood": mood_info,
        "due_open_loops": due_loops,
        "expired_open_loops_count": expired_count,
        "inner_life": inner_life,
    }


def ack_proactive_event(
    brain,
    character_id: str,
    *,
    open_loop_id: str = "",
    action: str = "nudged",
    snooze_hours: float = 24.0,
    reason: str = "",
    event_id: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Idempotently record open-loop dispatch state (`nudged`/`snooze`/`dismiss`/`reset`)."""
    act = str(action or "").strip().lower()
    if act not in VALID_ACK_ACTIONS:
        raise ValueError(
            f"Unsupported proactive ack action: {action!r} "
            f"(expected one of {', '.join(sorted(VALID_ACK_ACTIONS))})"
        )

    mid = str(open_loop_id or "").strip()
    if not mid:
        return {"ok": True, "recorded": False, "action": act, "reason": "no_open_loop_id"}

    engine = brain.engine
    ensure_proactive_tables(engine)
    ts = float(now) if now is not None else time.time()
    clean_event_id = str(event_id or "").strip()[:80]
    clean_reason = str(reason or "").strip()[:80]

    with engine._transaction():
        existing = engine.db.execute(
            "SELECT nudge_count, last_nudged_at, snoozed_until, auto_expired_at, "
            "resolved_reason, last_event_id "
            "FROM xiaozhi_open_loop_dispatch "
            "WHERE owner=? AND character_id=? AND memory_id=?",
            (engine.owner, character_id, mid),
        ).fetchone()
        cur = dict(existing) if existing is not None else {}

        if act == "nudged":
            if clean_event_id and cur.get("last_event_id") == clean_event_id:
                return {
                    "ok": True,
                    "recorded": True,
                    "idempotent_replay": True,
                    "action": act,
                    "open_loop_id": mid,
                    "nudge_count": int(cur.get("nudge_count") or 0),
                }
            new_count = int(cur.get("nudge_count") or 0) + 1
            engine.db.execute(
                "INSERT INTO xiaozhi_open_loop_dispatch("
                "owner, character_id, memory_id, nudge_count, last_nudged_at, "
                "snoozed_until, auto_expired_at, resolved_reason, last_event_id, updated_at"
                ") VALUES(?, ?, ?, ?, ?, 0, 0, '', ?, ?) "
                "ON CONFLICT(owner, character_id, memory_id) DO UPDATE SET "
                "nudge_count=excluded.nudge_count, last_nudged_at=excluded.last_nudged_at, "
                "last_event_id=excluded.last_event_id, updated_at=excluded.updated_at",
                (engine.owner, character_id, mid, new_count, ts, clean_event_id, ts),
            )
            return {
                "ok": True,
                "recorded": True,
                "idempotent_replay": False,
                "action": act,
                "open_loop_id": mid,
                "nudge_count": new_count,
                "last_nudged_at": ts,
            }

        if act == "snooze":
            if isinstance(snooze_hours, bool):
                raise ValueError("snooze_hours must be a positive finite number")
            try:
                raw_h = float(snooze_hours)
            except (TypeError, ValueError) as exc:
                raise ValueError("snooze_hours must be a positive finite number") from exc
            if not math.isfinite(raw_h) or raw_h <= 0.0:
                raise ValueError("snooze_hours must be a positive finite number")
            hours = max(1.0, min(168.0, raw_h))
            until_ts = ts + hours * 3600.0
            engine.db.execute(
                "INSERT INTO xiaozhi_open_loop_dispatch("
                "owner, character_id, memory_id, nudge_count, last_nudged_at, "
                "snoozed_until, auto_expired_at, resolved_reason, last_event_id, updated_at"
                ") VALUES(?, ?, ?, 0, 0, ?, 0, '', ?, ?) "
                "ON CONFLICT(owner, character_id, memory_id) DO UPDATE SET "
                "snoozed_until=excluded.snoozed_until, updated_at=excluded.updated_at",
                (engine.owner, character_id, mid, until_ts, clean_event_id, ts),
            )
            return {
                "ok": True,
                "recorded": True,
                "action": act,
                "open_loop_id": mid,
                "snoozed_until": until_ts,
            }

        if act == "dismiss":
            res_reason = clean_reason or "dismissed"
            engine.db.execute(
                "INSERT INTO xiaozhi_open_loop_dispatch("
                "owner, character_id, memory_id, nudge_count, last_nudged_at, "
                "snoozed_until, auto_expired_at, resolved_reason, last_event_id, updated_at"
                ") VALUES(?, ?, ?, 0, 0, 0, ?, ?, ?, ?) "
                "ON CONFLICT(owner, character_id, memory_id) DO UPDATE SET "
                "auto_expired_at=excluded.auto_expired_at, "
                "resolved_reason=excluded.resolved_reason, "
                "updated_at=excluded.updated_at",
                (engine.owner, character_id, mid, ts, res_reason, clean_event_id, ts),
            )
            return {
                "ok": True,
                "recorded": True,
                "action": act,
                "open_loop_id": mid,
                "resolved_reason": res_reason,
                "auto_expired_at": ts,
            }

        # act == "reset"
        engine.db.execute(
            "INSERT INTO xiaozhi_open_loop_dispatch("
            "owner, character_id, memory_id, nudge_count, last_nudged_at, "
            "snoozed_until, auto_expired_at, resolved_reason, last_event_id, updated_at"
            ") VALUES(?, ?, ?, 0, 0, 0, 0, '', '', ?) "
            "ON CONFLICT(owner, character_id, memory_id) DO UPDATE SET "
            "nudge_count=0, last_nudged_at=0, snoozed_until=0, "
            "auto_expired_at=0, resolved_reason='', last_event_id='', "
            "updated_at=excluded.updated_at",
            (engine.owner, character_id, mid, ts),
        )
        return {
            "ok": True,
            "recorded": True,
            "action": act,
            "open_loop_id": mid,
        }
