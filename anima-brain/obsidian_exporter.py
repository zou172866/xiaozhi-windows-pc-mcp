"""One-way Obsidian Markdown exporter for Anima Brain (runs inside anima-brain process).

Exports committed conversations, long-term facts (with collapsed revision history),
open loops, relationship & emotion snapshots, core notebooks, and episode summaries
directly into a clean dedicated Obsidian Vault (e.g. D:\\Obsidian\\XiaozhiMemory -> /obsidian).

Design guarantees:
- Reads brain.sqlite3 exclusively inside the anima-brain process under engine._lock
  (never opens WAL SQLite across the Windows <-> WSL2 VM boundary).
- Deterministic Markdown rendering + SHA-256 content deduplication + atomic temp-file replace.
- Startup backfill fills missing historical days; normal runtime only updates today
  (plus any past date receiving a delayed turn) while keeping past days frozen;
  manual full rebuild refreshes all dates while skipping byte-identical files.
- Reuses companion_engine.automation._SECRET_PATTERNS to redact accidental credentials.
- Degrades gracefully without crashing Anima Brain if the Vault directory is unavailable/read-only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from companion_engine import emotion
from companion_engine.automation import _SECRET_PATTERNS
from companion_engine.models import CharacterSpec

try:
    import recall_sanitizer as _recall_sanitizer
except Exception:
    _recall_sanitizer = None

log = logging.getLogger("anima-obsidian")

_EMOTION_TAG_RE = re.compile(
    r"\[(?:calm|gentle|happy|playful|shy|serious|tender)\]\s*",
    re.IGNORECASE,
)

_GENERATED_EPISODE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-片段-[0-9a-fA-F]{8,12}\.md$")

_CATEGORY_ORDER = (
    ("identity", "身份信息 identity"),
    ("preference", "偏好习惯 preference"),
    ("relationship", "人际与关系 relationship"),
    ("goal", "目标计划 goal"),
    ("event", "生活事件 event"),
    ("emotional", "情绪与感受 emotional"),
    ("date", "重要日期 date"),
    ("boundary", "边界与底线 boundary"),
    ("style", "沟通风格 style"),
)

_EMOTION_ZH = {
    "calm": "平静 calm",
    "warm": "温暖 warm",
    "happy": "开心 happy",
    "curious": "好奇 curious",
    "hurt": "委屈 hurt",
    "sad": "难过 sad",
    "guarded": "戒备 guarded",
    "playful": "俏皮 playful",
    "tender": "柔情 tender",
    "flustered": "害羞 flustered",
    "annoyed": "烦躁 annoyed",
    "anxious": "焦虑 anxious",
    "gentle": "温柔 gentle",
    "shy": "害羞 shy",
    "serious": "认真 serious",
}

_SOURCE_LABELS = {
    "esp32": "ESP32 语音",
    "studio": "Companion Studio",
}

_STAGE_LABELS_ZH = ("初识陌生", "礼貌熟识", "温柔知己", "亲密依恋")


def _looks_like_basic_auth_token(token: str) -> bool:
    """Return True if a bare `basic <token>` payload looks like HTTP Basic base64 credentials, not English prose."""
    if not token or len(token) < 8:
        return False
    if any(ch in token for ch in ("=", "+", "/", ":")):
        return True
    if len(token) < 12:
        return False
    try:
        padded = token + ("=" * ((4 - len(token) % 4) % 4))
        decoded = base64.b64decode(padded, validate=True).decode("utf-8", errors="strict")
        return ":" in decoded and all(32 <= ord(c) < 127 for c in decoded)
    except Exception:
        return False


def redact_credentials(text: str) -> str:
    """Redact accidental API keys, bearer tokens, and passwords without altering normal chat."""
    if not text:
        return ""
    out = str(text)
    for pat, repl in _SECRET_PATTERNS:
        if "bearer|basic" in pat.pattern.lower():
            def _sub_auth(m: re.Match[str], _repl: str = repl) -> str:
                prefix = m.group(1)
                prefix_low = prefix.lower()
                if "basic" in prefix_low and "authorization" not in prefix_low:
                    payload = m.group(0)[len(prefix):]
                    if not _looks_like_basic_auth_token(payload):
                        return m.group(0)
                return m.expand(_repl)

            out = pat.sub(_sub_auth, out)
        else:
            out = pat.sub(repl, out)
    return out


def _single_line_text(text: str) -> str:
    """Redact credentials and collapse newlines/excess whitespace for single-line bullets & callouts."""
    return " ".join(redact_credentials(str(text or "")).split())


_LINE_BREAK_RE = re.compile(r"\r\n|[\r\x0b\x0c\x85\u2028\u2029]")
_YAML_RESERVED_LITERALS = frozenset({"null", "~", "true", "false", "yes", "no", "on", "off"})
_YAML_NUMERIC_RE = re.compile(
    r"^(?:[+-]?(?:0x[0-9a-fA-F_]+|0o[0-7_]+|\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?\d+)?|\.\d[\d_]*(?:[eE][+-]?\d+)?)"
    r"|[+-]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)
_YAML_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:$|[Tt ])")
_UNSAFE_INLINE_HTML_RE = re.compile(r"<(?=(?:!--|/?(?:script|style|iframe|textarea)\b))", re.IGNORECASE)
_BACKTICK_RUN_RE = re.compile(r"`+")
_PCT_PAIR_RE = re.compile(r"%%")
_DOLLAR_PAIR_RE = re.compile(r"\$\$")


def _count_preceding_backslashes(s: str, pos: int) -> int:
    n = 0
    i = pos - 1
    while i >= 0 and s[i] == "\\":
        n += 1
        i -= 1
    return n


def _neutralize_dialogue_block_breaker(line: str, *, is_first: bool = False) -> str:
    """Escape Markdown block-level starters so a multi-line turn stays one Obsidian paragraph for ^t-xxxxxxxx."""
    m = re.match(r"^([ \t]*)(.*)$", line)
    if not m:
        return line
    indent, rest = m.group(1), m.group(2)
    if not rest:
        return line
    # Fenced code blocks & display math blocks are neutralized even on line 0 to avoid unclosed blocks/spans.
    fm = re.match(r"^(`{3,}|~{3,})(.*)$", rest)
    if fm:
        return indent + "".join("\\" + ch for ch in fm.group(1)) + fm.group(2)
    if rest.startswith("$$"):
        return indent + rest.replace("$$", r"\$\$")
    if is_first:
        return line
    # Thematic breaks made of * or _ (escape every marker char so remaining **/__ cannot trigger emphasis pairing).
    if re.match(r"^(?:\*[ \t]*){3,}$", rest):
        return indent + rest.replace("*", r"\*")
    if re.match(r"^(?:_[ \t]*){3,}$", rest):
        return indent + rest.replace("_", r"\_")
    # ATX headings (# to ###### followed by whitespace or EOL; keeps #tags intact).
    if re.match(r"^#{1,6}(?:[ \t]|$)", rest):
        return indent + "\\" + rest
    # Blockquotes & Obsidian callouts (>).
    if rest.startswith(">"):
        return indent + "\\" + rest
    # Unordered lists (- / * / +), thematic breaks (- - -), Setext underlines (- / --- / = / ===),
    # and GFM table rows / delimiter rows (|... or :--- | ---:).
    if re.match(r"^(?:[-*+](?:[ \t]|$)|(?:-[ \t]*){2,}$|=+[ \t]*$|\||[:|-][\s:|-]*$)", rest):
        return indent + "\\" + rest
    # Ordered lists (1. / 1)).
    om = re.match(r"^(\d{1,9})([.)])(?:[ \t]|$)", rest)
    if om:
        return indent + f"{om.group(1)}\\{om.group(2)}{rest[om.end(2):]}"
    # HTML blocks (<...)
    if rest.startswith("<"):
        return indent + "\\" + rest
    # Footnote definitions ([^id]:)
    if re.match(r"^\[\^[^\]]+\]:", rest):
        return indent + "\\" + rest
    # Literal block ID line (^id) or Obsidian comment line (%%)
    if rest.startswith("^"):
        return indent + "\\" + rest
    if rest.startswith("%%"):
        return indent + r"\%\%" + rest[2:]
    return line


def _escape_plain_dialogue_segment(seg: str) -> str:
    """Escape Obsidian/HTML span-swallowing tokens (%% / <!-- / raw-text tags) outside inline code spans."""
    if not seg:
        return seg
    # Escape unescaped %% (Obsidian comments can swallow ^t-xxxxxxxx and subsequent turns across lines).
    seg = _PCT_PAIR_RE.sub(
        lambda m: r"\%\%" if (_count_preceding_backslashes(seg, m.start()) % 2 == 0) else m.group(0),
        seg,
    )
    # Escape HTML comments (<!--) and raw-text HTML tags (<script>/<style>/<iframe/<textarea) even on line 0 / mid-line.
    seg = _UNSAFE_INLINE_HTML_RE.sub(
        lambda m: r"\<" if (_count_preceding_backslashes(seg, m.start()) % 2 == 0) else m.group(0),
        seg,
    )
    return seg


def _escape_unbalanced_inline_delimiters(text: str) -> str:
    """Preserve balanced inline `code` and single-line $$math$$ while neutralizing cross-line/unclosed delimiters."""
    runs = list(_BACKTICK_RUN_RE.finditer(text))
    parts: list[tuple[bool, str]] = []  # (is_code_span, chunk)
    cursor = 0
    idx = 0
    while idx < len(runs):
        m = runs[idx]
        if m.start() < cursor:
            idx += 1
            continue
        # Skip already backslash-escaped backtick runs (e.g. \`\`\`).
        if _count_preceding_backslashes(text, m.start()) % 2 == 1:
            idx += 1
            continue
        run_len = m.end() - m.start()
        # Look ahead for a CommonMark closing backtick run of the exact same length within this utterance.
        close_idx = None
        for j in range(idx + 1, len(runs)):
            if (runs[j].end() - runs[j].start()) == run_len:
                close_idx = j
                break
        if m.start() > cursor:
            parts.append((False, text[cursor : m.start()]))
        if close_idx is not None:
            close_m = runs[close_idx]
            parts.append((True, text[m.start() : close_m.end()]))
            cursor = close_m.end()
            idx = close_idx + 1
        else:
            # Unclosed backtick run could pair across `\n**芽衣**` with backticks in the other utterance.
            parts.append((False, "".join("\\" + ch for ch in m.group(0))))
            cursor = m.end()
            idx += 1
    if cursor < len(text):
        parts.append((False, text[cursor:]))

    # Apply %% and unsafe inline HTML escaping only to non-code segments, then balance per-line $$ outside code spans.
    line_segments: list[list[tuple[bool, str]]] = [[]]
    for is_code, chunk in parts:
        processed = chunk if is_code else _escape_plain_dialogue_segment(chunk)
        sub_lines = processed.split("\n")
        for s_idx, sub in enumerate(sub_lines):
            if s_idx > 0:
                line_segments.append([])
            if sub:
                line_segments[-1].append((is_code, sub))

    out_lines: list[str] = []
    for segs in line_segments:
        # Count unescaped $$ outside inline code spans on this line; odd count means unclosed or cross-line $$ math.
        dollar_count = 0
        for is_code, sub in segs:
            if not is_code:
                for dm in _DOLLAR_PAIR_RE.finditer(sub):
                    if _count_preceding_backslashes(sub, dm.start()) % 2 == 0:
                        dollar_count += 1
        if dollar_count % 2 == 1:
            fixed_segs: list[str] = []
            for is_code, sub in segs:
                if is_code:
                    fixed_segs.append(sub)
                else:
                    fixed_segs.append(
                        _DOLLAR_PAIR_RE.sub(
                            lambda m, _s=sub: r"\$\$" if (_count_preceding_backslashes(_s, m.start()) % 2 == 0) else m.group(0),
                            sub,
                        )
                    )
            line_str = "".join(fixed_segs)
        else:
            line_str = "".join(sub for _, sub in segs)

        # Avoid trailing odd backslash escaping `\n` right before `^t-xxxxxxxx` or `**speaker**`.
        if line_str.endswith("\\") and (_count_preceding_backslashes(line_str, len(line_str)) % 2 == 1):
            line_str += "\\"
        out_lines.append(line_str)

    return "\n".join(out_lines)


def clean_dialogue_text(text: str, *, escape_block_breakers: bool = True) -> str:
    """Strip leaked emotion tags, redact credentials, and keep multi-line turns inside one Obsidian block."""
    s = _LINE_BREAK_RE.sub("\n", str(text or ""))
    s = _EMOTION_TAG_RE.sub("", s)
    s = redact_credentials(s)
    # Collapse blank lines inside a single utterance so ^t-xxxxxxxx attaches to the entire turn block
    lines = [ln.rstrip() for ln in s.split("\n")]
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return ""
    if escape_block_breakers:
        non_empty = [
            _neutralize_dialogue_block_breaker(ln, is_first=(idx == 0))
            for idx, ln in enumerate(non_empty)
        ]
        joined = "\n".join(non_empty).strip()
        return _escape_unbalanced_inline_delimiters(joined)
    return "\n".join(non_empty).strip()


def _yaml_scalar(value: Any) -> str:
    s = str(value or "")
    if (
        not s
        or s != s.strip()
        or s == "..."
        or s[0] in ("-", "?", "!", "&", "*", "%", "@", "`", "|", ">")
        or s.lower() in _YAML_RESERVED_LITERALS
        or bool(_YAML_NUMERIC_RE.match(s))
        or bool(_YAML_DATE_PREFIX_RE.match(s))
        or any(
            ord(ch) < 32
            or ord(ch) == 127
            or ch in (":", "#", "[", "]", "{", "}", ",", '"', "'")
            for ch in s
        )
    ):
        return json.dumps(s, ensure_ascii=False)
    return s


def _yaml_optional_updated(ts: float | None, tz: ZoneInfo) -> str:
    """Format an optional frontmatter timestamp as clean YAML (ISO minute string, or null when unset)."""
    formatted = _fmt_iso_min(ts, tz) if ts and float(ts) > 0 else ""
    return _yaml_scalar(formatted) if formatted else "null"


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(_yaml_scalar(x) for x in items) + "]"


def _resolve_tz(tz_name: str | None) -> ZoneInfo:
    for cand in (tz_name, "Asia/Shanghai", "UTC"):
        if not cand:
            continue
        try:
            return ZoneInfo(str(cand))
        except Exception:
            continue
    return ZoneInfo("UTC")


def _fmt_date(ts: float | None, tz: ZoneInfo) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _fmt_time(ts: float | None, tz: ZoneInfo) -> str:
    if not ts:
        return "00:00:00"
    try:
        return datetime.fromtimestamp(float(ts), tz).strftime("%H:%M:%S")
    except Exception:
        return "00:00:00"


def _fmt_iso_min(ts: float | None, tz: ZoneInfo) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return ""


def _fmt_iso_sec(ts: float | None, tz: ZoneInfo) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _dialogue_rel_path(date_str: str) -> str:
    parts = date_str.split("-")
    if len(parts) == 3:
        return f"对话/{parts[0]}/{parts[1]}/{date_str}"
    return f"对话/{date_str}"


def _short_speaker_name(character_name: str) -> str:
    name = (character_name or "").strip() or "小智"
    if "芽衣" in name:
        return "芽衣"
    return name


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_if_changed(path: Path, content: str) -> str:
    """Atomically write UTF-8 text if SHA-256 differs from existing file. Returns 'written' or 'skipped'."""
    raw = content.encode("utf-8")
    new_hash = _sha256_bytes(raw)
    if path.is_file():
        try:
            old_raw = path.read_bytes()
            if _sha256_bytes(old_raw) == new_hash:
                return "skipped"
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(raw)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        return "written"
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _probe_writable(export_dir: Path) -> tuple[bool, str]:
    try:
        if export_dir.exists() and not export_dir.is_dir():
            return False, f"导出路径不是目录: {export_dir}"
        export_dir.mkdir(parents=True, exist_ok=True)
        probe = export_dir / f".obsidian_probe_{os.getpid()}_{threading.get_ident()}.tmp"
        with open(probe, "wb") as f:
            f.write(b"ok")
            f.flush()
        probe.unlink(missing_ok=True)
        return True, ""
    except Exception as exc:
        return False, redact_credentials(str(exc))[:200]


def _bar(ratio: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(ratio * width))))
    return "█" * filled + "░" * (width - filled)


class ObsidianExporter:
    """Reads Anima Brain SQLite under engine._lock and writes clean Obsidian Markdown files."""

    def __init__(
        self,
        brain: Any,
        *,
        character_id: str = "xiaozhi",
        export_dir: str | Path = "/obsidian",
        host_export_dir: str = "",
        config_path: str | Path | None = None,
        default_enabled: bool = False,
        snapshot_interval_seconds: int = 600,
        default_briefing_enabled: bool = True,
        briefing_interval_seconds: int = 3600,
    ):
        self.brain = brain
        self.character_id = character_id
        self.export_dir = Path(export_dir)
        self.host_export_dir = (host_export_dir or "").strip() or str(self.export_dir)
        self.config_path = Path(config_path) if config_path else None
        self.default_enabled = bool(default_enabled)
        self.snapshot_interval_seconds = max(15, int(snapshot_interval_seconds))
        self.default_briefing_enabled = bool(default_briefing_enabled)
        self.briefing_interval_seconds = max(300, int(briefing_interval_seconds))

        self._op_lock = threading.RLock()
        self._enabled, self._briefing_enabled = self._load_settings()
        self._writable: bool | None = None
        self._last_error: str = ""
        self._last_export_ts: float = 0.0
        self._last_export_at: str = ""
        self._last_export_reason: str = ""
        self._last_briefing_ts: float = 0.0
        self._last_briefing_at: str = ""
        self._last_briefing_reason: str = ""
        self._last_briefing_daily_date: str = ""
        self._last_briefing_week_key: str = ""
        self._last_briefing_stats: dict[str, Any] = {
            "written": 0,
            "skipped": 0,
            "daily_turns": 0,
            "weekly_turns": 0,
            "open_loops": 0,
            "urgent_loops": 0,
        }
        self._last_fingerprint: str = ""
        self._last_export_seq: int | None = None
        self._seq_triggers_ready: bool = False
        self._seq_triggers_has_meta: bool = False
        self._known_turn_sigs: dict[str, tuple[str, str, int]] = {}  # turn_id -> (date_str, content_sha8, used_tools)
        self._last_stats: dict[str, Any] = {
            "turns": 0,
            "days": 0,
            "active_facts": 0,
            "historical_revisions": 0,
            "open_loops": 0,
            "episodes": 0,
            "files_written": 0,
            "files_skipped": 0,
        }

    def _load_settings(self) -> tuple[bool, bool]:
        en = self.default_enabled
        br = self.default_briefing_enabled
        if self.config_path and self.config_path.is_file():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if isinstance(data.get("enabled"), bool):
                        en = bool(data["enabled"])
                    if isinstance(data.get("briefing_enabled"), bool):
                        br = bool(data["briefing_enabled"])
            except Exception:
                pass
        return en, br

    def _persist_settings_locked(self) -> None:
        if self.config_path is None:
            return
        try:
            _atomic_write_if_changed(
                self.config_path,
                json.dumps(
                    {
                        "enabled": self._enabled,
                        "briefing_enabled": self._briefing_enabled,
                        "updated_at": round(time.time(), 3),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        except OSError as exc:
            log.warning("Failed to persist obsidian_sync_config.json: %s", exc)

    def is_enabled(self) -> bool:
        return bool(self._enabled)

    def is_briefing_enabled(self) -> bool:
        return bool(self._briefing_enabled)

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._op_lock:
            self._enabled = bool(enabled)
            self._persist_settings_locked()
        return self.status()

    def set_briefing_enabled(self, briefing_enabled: bool) -> dict[str, Any]:
        with self._op_lock:
            self._briefing_enabled = bool(briefing_enabled)
            self._persist_settings_locked()
        return self.status()

    def _has_turn_meta_table(self, db) -> bool:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xiaozhi_turn_meta'"
        ).fetchone()
        return row is not None

    def _ensure_change_seq_triggers_locked(self, db, *, has_meta: bool) -> int:
        """Install lightweight monotonic SQLite triggers so `sync_if_db_changed` polls in O(1) (P1-5)."""
        if not self._seq_triggers_ready or (has_meta and not self._seq_triggers_has_meta):
            db.execute(
                "CREATE TABLE IF NOT EXISTS xiaozhi_export_seq ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "seq INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("INSERT OR IGNORE INTO xiaozhi_export_seq(id, seq) VALUES(1, 0)")
            tables = [
                "characters",
                "turns",
                "turn_appraisals",
                "memories",
                "memory_details",
                "fact_lifecycle",
                "revisions",
                "documents",
                "document_metadata",
                "notebooks",
            ]
            if has_meta:
                tables.append("xiaozhi_turn_meta")
            existing_tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for tbl in tables:
                if tbl not in existing_tables:
                    continue
                for op in ("INSERT", "UPDATE", "DELETE"):
                    trig_name = f"xz_exp_seq_{tbl}_{op.lower()}"
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {trig_name} AFTER {op} ON {tbl} "
                        "BEGIN UPDATE xiaozhi_export_seq SET seq = seq + 1 WHERE id = 1; END"
                    )
            self._seq_triggers_ready = True
            if has_meta:
                self._seq_triggers_has_meta = True
        row = db.execute("SELECT seq FROM xiaozhi_export_seq WHERE id = 1").fetchone()
        return int(row[0]) if row is not None else 0

    def _read_export_seq(self) -> int | None:
        try:
            engine = self.brain.engine
            with engine._lock:
                has_meta = self._has_turn_meta_table(engine.db)
                return self._ensure_change_seq_triggers_locked(engine.db, has_meta=has_meta)
        except Exception:
            return None

    def _read_snapshot_data(self) -> dict[str, Any]:
        """Take a single consistent read-only snapshot of character data under engine._lock."""
        engine = self.brain.engine
        with engine._lock:
            char_row = engine.db.execute(
                "SELECT * FROM characters WHERE owner=? AND id=?",
                (engine.owner, self.character_id),
            ).fetchone()
            if char_row is None:
                raise ValueError(f"Character {self.character_id!r} not found")
            spec = CharacterSpec.model_validate_json(char_row["spec"])
            state = json.loads(char_row["state"])
            tz = _resolve_tz(spec.timezone)
            has_meta = self._has_turn_meta_table(engine.db)
            export_seq = self._ensure_change_seq_triggers_locked(engine.db, has_meta=has_meta)

            if has_meta:
                turn_rows = engine.db.execute(
                    "SELECT t.id, t.request_id, t.user_message, t.assistant_reply, "
                    "t.created_at, t.rowid AS _rowid, "
                    "m.occurred_at AS meta_occurred_at, m.source AS meta_source, "
                    "m.device_id AS meta_device_id, m.used_tools AS meta_used_tools, "
                    "a.data AS appraisal_json "
                    "FROM turns t "
                    "LEFT JOIN xiaozhi_turn_meta m ON m.turn_id = t.id "
                    "LEFT JOIN turn_appraisals a ON a.turn_id = t.id "
                    "WHERE t.owner=? AND t.character_id=? AND t.status='committed' "
                    "ORDER BY coalesce(m.occurred_at, t.created_at) ASC, t.created_at ASC, t.rowid ASC",
                    (engine.owner, self.character_id),
                ).fetchall()
            else:
                turn_rows = engine.db.execute(
                    "SELECT t.id, t.request_id, t.user_message, t.assistant_reply, "
                    "t.created_at, t.rowid AS _rowid, "
                    "NULL AS meta_occurred_at, NULL AS meta_source, "
                    "'' AS meta_device_id, 0 AS meta_used_tools, "
                    "a.data AS appraisal_json "
                    "FROM turns t "
                    "LEFT JOIN turn_appraisals a ON a.turn_id = t.id "
                    "WHERE t.owner=? AND t.character_id=? AND t.status='committed' "
                    "ORDER BY t.created_at ASC, t.rowid ASC",
                    (engine.owner, self.character_id),
                ).fetchall()

            turns: list[dict[str, Any]] = []
            for r in turn_rows:
                occ = r["meta_occurred_at"]
                eff_ts = float(occ) if occ is not None and float(occ) > 0 else float(r["created_at"])
                raw_src = (r["meta_source"] or "").strip()
                if not raw_src:
                    raw_src = "esp32" if str(r["request_id"] or "").startswith("xz-") else "studio"
                app_data = {}
                if r["appraisal_json"]:
                    try:
                        app_data = json.loads(r["appraisal_json"])
                    except Exception:
                        app_data = {}
                date_str = _fmt_date(eff_ts, tz)
                turns.append(
                    {
                        "id": str(r["id"]),
                        "request_id": str(r["request_id"] or ""),
                        "user_message": str(r["user_message"] or ""),
                        "assistant_reply": str(r["assistant_reply"] or ""),
                        "created_at": float(r["created_at"]),
                        "effective_ts": eff_ts,
                        "date": date_str,
                        "time": _fmt_time(eff_ts, tz),
                        "source": raw_src,
                        "device_id": str(r["meta_device_id"] or ""),
                        "used_tools": bool(r["meta_used_tools"]),
                        "appraisal_raw": str(r["appraisal_json"] or ""),
                        "emotion": str(app_data.get("emotion") or "").strip(),
                        "intensity": int(app_data.get("intensity") or 0),
                    }
                )

            mem_rows = engine.db.execute(
                "SELECT * FROM memories WHERE owner=? AND character_id=? ORDER BY updated_at DESC, id ASC",
                (engine.owner, self.character_id),
            ).fetchall()
            memories = [engine._memory(r) for r in mem_rows]
            mem_by_id = {m.id: m for m in memories}

            rev_rows = engine.db.execute(
                "SELECT r.memory_id, r.revision, r.previous, r.changed_at "
                "FROM revisions r JOIN memories m ON m.id = r.memory_id "
                "WHERE m.owner=? AND m.character_id=? "
                "ORDER BY r.changed_at DESC, r.revision DESC",
                (engine.owner, self.character_id),
            ).fetchall()
            revisions = [dict(r) for r in rev_rows]

            doc_rows = engine.db.execute(
                "SELECT d.id, d.kind, d.text, d.sources, d.created_at, "
                "dm.data AS meta_json, nb.data AS notebook_json "
                "FROM documents d "
                "LEFT JOIN document_metadata dm ON dm.document_id = d.id "
                "LEFT JOIN notebooks nb ON nb.document_id = d.id "
                "WHERE d.owner=? AND d.character_id=? "
                "ORDER BY d.created_at DESC, d.id ASC",
                (engine.owner, self.character_id),
            ).fetchall()
            documents = []
            for r in doc_rows:
                try:
                    sources = json.loads(r["sources"] or "[]")
                except Exception:
                    sources = []
                try:
                    meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
                except Exception:
                    meta = {}
                try:
                    nb = json.loads(r["notebook_json"]) if r["notebook_json"] else None
                except Exception:
                    nb = None
                documents.append(
                    {
                        "id": str(r["id"]),
                        "kind": str(r["kind"]),
                        "text": str(r["text"] or ""),
                        "sources": sources if isinstance(sources, list) else [],
                        "created_at": float(r["created_at"] or 0.0),
                        "meta": meta if isinstance(meta, dict) else {},
                        "notebook": nb,
                    }
                )

            app_row = engine.db.execute(
                "SELECT count(*) FROM turn_appraisals a JOIN turns t ON t.id=a.turn_id "
                "WHERE t.owner=? AND t.character_id=?",
                (engine.owner, self.character_id),
            ).fetchone()

            return {
                "owner": engine.owner,
                "raw_spec": char_row["spec"],
                "raw_state": char_row["state"],
                "export_seq": export_seq,
                "app_count": int(app_row[0]) if app_row else 0,
                "spec": spec,
                "state": state,
                "tz": tz,
                "turns": turns,
                "memories": memories,
                "mem_by_id": mem_by_id,
                "revisions": revisions,
                "documents": documents,
            }

    def _compute_fingerprint_from_snapshot(
        self, snap: dict[str, Any]
    ) -> tuple[str, dict[str, tuple[str, str, int]]]:
        """Derive `(fingerprint, turn_sigs)` in memory from an already-loaded snapshot (zero extra DB locks, P1-5)."""
        turn_sigs: dict[str, tuple[str, str, int]] = {}
        t_digest_parts = []
        for t in snap["turns"]:
            d_str = t["date"]
            c_hash = hashlib.sha256(
                f"{t['user_message']}\0{t['assistant_reply']}\0{t.get('appraisal_raw', '')}".encode("utf-8")
            ).hexdigest()[:12]
            sig = (d_str, c_hash, 1 if t["used_tools"] else 0)
            turn_sigs[t["id"]] = sig
            t_digest_parts.append(f"{t['id']}:{sig[0]}:{sig[1]}:{sig[2]}")
        t_digest_parts.sort()

        memories = snap["memories"]
        revisions = snap["revisions"]
        documents = snap["documents"]
        active_mem_count = sum(1 for m in memories if not m.lifecycle.invalidation)
        open_loop_count = sum(
            1 for m in memories if not m.lifecycle.invalidation and m.details.loop_status == "open"
        )
        mem_sig = (
            len(memories),
            max((m.updated_at for m in memories), default=0),
            sum((m.revision for m in memories), 0),
            active_mem_count,
            open_loop_count,
        )
        rev_sig = (
            len(revisions),
            max((r["changed_at"] for r in revisions), default=0),
        )
        doc_sig = (
            len(documents),
            max((d["created_at"] for d in documents), default=0),
        )
        app_sig = (int(snap.get("app_count", 0)),)
        raw_sig = json.dumps(
            {
                "spec": snap["raw_spec"],
                "state": snap["raw_state"],
                "turns": hashlib.sha256("|".join(t_digest_parts).encode("utf-8")).hexdigest(),
                "mem": mem_sig,
                "rev": rev_sig,
                "doc": doc_sig,
                "app": app_sig,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw_sig.encode("utf-8")).hexdigest(), turn_sigs

    def compute_db_fingerprint(self) -> tuple[str, dict[str, tuple[str, str, int]]]:
        """Cheap read-only fingerprint of DB state + turn_id -> (date, content_sha8, used_tools) map."""
        engine = self.brain.engine
        with engine._lock:
            char_row = engine.db.execute(
                "SELECT spec, state FROM characters WHERE owner=? AND id=?",
                (engine.owner, self.character_id),
            ).fetchone()
            if char_row is None:
                return "", {}
            spec = CharacterSpec.model_validate_json(char_row["spec"])
            tz = _resolve_tz(spec.timezone)
            has_meta = self._has_turn_meta_table(engine.db)

            if has_meta:
                t_rows = engine.db.execute(
                    "SELECT t.id, coalesce(m.occurred_at, t.created_at) AS ts, "
                    "coalesce(t.user_message, '') AS u_msg, "
                    "coalesce(t.assistant_reply, '') AS a_msg, "
                    "coalesce(m.used_tools, 0) AS ut, "
                    "coalesce(a.data, '') AS app_json "
                    "FROM turns t "
                    "LEFT JOIN xiaozhi_turn_meta m ON m.turn_id = t.id "
                    "LEFT JOIN turn_appraisals a ON a.turn_id = t.id "
                    "WHERE t.owner=? AND t.character_id=? AND t.status='committed'",
                    (engine.owner, self.character_id),
                ).fetchall()
            else:
                t_rows = engine.db.execute(
                    "SELECT t.id, t.created_at AS ts, "
                    "coalesce(t.user_message, '') AS u_msg, "
                    "coalesce(t.assistant_reply, '') AS a_msg, "
                    "0 AS ut, "
                    "coalesce(a.data, '') AS app_json "
                    "FROM turns t "
                    "LEFT JOIN turn_appraisals a ON a.turn_id = t.id "
                    "WHERE t.owner=? AND t.character_id=? AND t.status='committed'",
                    (engine.owner, self.character_id),
                ).fetchall()

            turn_sigs: dict[str, tuple[str, str, int]] = {}
            t_digest_parts = []
            for r in t_rows:
                d_str = _fmt_date(r["ts"], tz)
                c_hash = hashlib.sha256(
                    f"{r['u_msg']}\0{r['a_msg']}\0{r['app_json']}".encode("utf-8")
                ).hexdigest()[:12]
                sig = (d_str, c_hash, int(r["ut"] or 0))
                turn_sigs[str(r["id"])] = sig
                t_digest_parts.append(f"{r['id']}:{sig[0]}:{sig[1]}:{sig[2]}")
            t_digest_parts.sort()

            mem_sig = engine.db.execute(
                "SELECT count(*), coalesce(max(m.updated_at), 0), coalesce(sum(m.revision), 0), "
                "coalesce(sum(CASE WHEN json_extract(l.data, '$.invalidation') IS NULL THEN 1 ELSE 0 END), 0), "
                "coalesce(sum(CASE WHEN json_extract(l.data, '$.invalidation') IS NULL "
                "AND json_extract(d.data, '$.loop_status') = 'open' THEN 1 ELSE 0 END), 0) "
                "FROM memories m "
                "LEFT JOIN fact_lifecycle l ON l.memory_id = m.id "
                "LEFT JOIN memory_details d ON d.memory_id = m.id "
                "WHERE m.owner=? AND m.character_id=?",
                (engine.owner, self.character_id),
            ).fetchone()
            rev_sig = engine.db.execute(
                "SELECT count(*), coalesce(max(r.changed_at), 0) "
                "FROM revisions r JOIN memories m ON m.id=r.memory_id "
                "WHERE m.owner=? AND m.character_id=?",
                (engine.owner, self.character_id),
            ).fetchone()
            doc_sig = engine.db.execute(
                "SELECT count(*), coalesce(max(created_at), 0) "
                "FROM documents WHERE owner=? AND character_id=?",
                (engine.owner, self.character_id),
            ).fetchone()
            app_sig = engine.db.execute(
                "SELECT count(*) FROM turn_appraisals a JOIN turns t ON t.id=a.turn_id "
                "WHERE t.owner=? AND t.character_id=?",
                (engine.owner, self.character_id),
            ).fetchone()

            raw_sig = json.dumps(
                {
                    "spec": char_row["spec"],
                    "state": char_row["state"],
                    "turns": hashlib.sha256("|".join(t_digest_parts).encode("utf-8")).hexdigest(),
                    "mem": tuple(mem_sig),
                    "rev": tuple(rev_sig),
                    "doc": tuple(doc_sig),
                    "app": tuple(app_sig),
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            return hashlib.sha256(raw_sig.encode("utf-8")).hexdigest(), turn_sigs

    def _build_turn_index(self, turns: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, tuple[str, str]]]:
        """Group turns by date and assign collision-free ^t-<8char> block IDs."""
        by_date: dict[str, list[dict[str, Any]]] = {}
        for t in turns:
            by_date.setdefault(t["date"], []).append(t)

        block_map: dict[str, tuple[str, str]] = {}
        for date_str, day_turns in by_date.items():
            seen_ids: set[str] = set()
            for t in day_turns:
                tid_clean = re.sub(r"[^a-zA-Z0-9]", "", t["id"]).lower()
                cand = f"t-{tid_clean[:8]}"
                if cand in seen_ids:
                    cand = f"t-{tid_clean[:12]}"
                if cand in seen_ids:
                    cand = f"t-{tid_clean}"
                seen_ids.add(cand)
                t["block_id"] = cand
                block_map[t["id"]] = (date_str, cand)
        return by_date, block_map

    def _turn_wikilink(
        self, turn_id: str | None, block_map: dict[str, tuple[str, str]], label: str = "原话"
    ) -> str:
        if not turn_id or turn_id not in block_map:
            return ""
        d_str, blk = block_map[turn_id]
        rel = _dialogue_rel_path(d_str)
        return f"[[{rel}#^{blk}|{label}]]"

    def _render_daily_dialogue(
        self, date_str: str, day_turns: list[dict[str, Any]], spec: CharacterSpec
    ) -> str:
        sources = sorted({t["source"] for t in day_turns if t.get("source")})
        mood_counts = Counter(t["emotion"] for t in day_turns if t.get("emotion"))
        moods = [m for m, _ in mood_counts.most_common()]
        speaker = _short_speaker_name(spec.name)

        lines = [
            "---",
            "type: 小智对话日志",
            f"date: {date_str}",
            f"character: {_yaml_scalar(spec.name)}",
            f"turns: {len(day_turns)}",
            f"sources: {_yaml_list(sources)}",
            f"moods: {_yaml_list(moods)}",
            "tags: [小智/对话]",
            "---",
            "",
            "> [!info] 自动生成，由 anima-brain 按数据库重建。日常历史日默认冻结；在控制台点「立即全量重建」时会按最新模板重刷。",
            "",
        ]

        for t in day_turns:
            src_label = _SOURCE_LABELS.get(t["source"], t["source"])
            tool_badge = " · 🔧 调用工具" if t.get("used_tools") else ""
            lines.append(f"## {t['time']} · {src_label}{tool_badge}")
            u_txt = clean_dialogue_text(t["user_message"])
            a_txt = clean_dialogue_text(t["assistant_reply"])
            emo = t.get("emotion") or ""
            emo_part = f" · {emo}" if emo else ""
            lines.append(f"**我**：{u_txt}")
            lines.append(f"**{speaker}**{emo_part}：{a_txt}")
            lines.append(f"^{t['block_id']}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _resolve_active_replacement_text(self, mem_id: str | None, mem_by_id: dict[str, Any]) -> str:
        seen: set[str] = set()
        cur = mem_by_id.get(mem_id) if mem_id else None
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            if not cur.lifecycle.invalidation:
                return _single_line_text(cur.text)
            nxt_id = cur.lifecycle.superseded_by
            cur = mem_by_id.get(nxt_id) if nxt_id else None
        return ""

    def _collect_historical_revisions(
        self, snap: dict[str, Any], block_map: dict[str, tuple[str, str]]
    ) -> list[dict[str, Any]]:
        tz = snap["tz"]
        mem_by_id = snap["mem_by_id"]
        items: list[dict[str, Any]] = []

        # 1. In-place revisions from `revisions` table
        for rev in snap["revisions"]:
            try:
                prev = json.loads(rev["previous"])
            except Exception:
                continue
            old_text = _single_line_text(prev.get("text") or "")
            if not old_text:
                continue
            inv = str(prev.get("invalidation") or "corrected")
            reason_zh = "已更正" if inv == "corrected" else "已替代"
            changed_ts = float(rev.get("changed_at") or prev.get("updated_at") or 0.0)
            cur_text = self._resolve_active_replacement_text(rev["memory_id"], mem_by_id)
            link = self._turn_wikilink(prev.get("source_turn_id"), block_map, "原话")
            items.append(
                {
                    "ts": changed_ts,
                    "old_text": old_text,
                    "reason_zh": reason_zh,
                    "rev_label": f"rev {rev['revision']}",
                    "date_str": _fmt_date(changed_ts, tz),
                    "current_text": cur_text,
                    "link": link,
                }
            )

        # 2. Cross-key retired memories from `fact_lifecycle.invalidation`
        for m in snap["memories"]:
            inv = m.lifecycle.invalidation
            if not inv:
                continue
            old_text = _single_line_text(m.text)
            if not old_text:
                continue
            reason_zh = "已更正" if inv == "corrected" else "已替代"
            changed_ts = float(m.lifecycle.invalidated_at or m.updated_at or 0.0)
            cur_text = self._resolve_active_replacement_text(m.lifecycle.superseded_by, mem_by_id)
            link = self._turn_wikilink(m.source_turn_id, block_map, "原话")
            items.append(
                {
                    "ts": changed_ts,
                    "old_text": old_text,
                    "reason_zh": reason_zh,
                    "rev_label": f"rev {m.revision}",
                    "date_str": _fmt_date(changed_ts, tz),
                    "current_text": cur_text,
                    "link": link,
                }
            )

        items.sort(key=lambda x: (x["ts"], x["old_text"]), reverse=True)
        return items

    def _render_memories_note(
        self, snap: dict[str, Any], block_map: dict[str, tuple[str, str]]
    ) -> tuple[str, int, int]:
        spec = snap["spec"]
        tz = snap["tz"]
        active_mems = [m for m in snap["memories"] if not m.lifecycle.invalidation]
        hist_revs = self._collect_historical_revisions(snap, block_map)
        open_loops_count = sum(1 for m in active_mems if m.details.loop_status == "open")

        max_ts = max(
            [m.updated_at for m in snap["memories"]]
            + [float(r.get("changed_at") or 0.0) for r in snap["revisions"]]
            + [0.0]
        )

        lines = [
            "---",
            "type: 小智长期记忆快照",
            f"character: {_yaml_scalar(spec.name)}",
            f"updated: {_yaml_optional_updated(max_ts, tz)}",
            f"facts: {len(active_mems)}",
            f"historical_revisions: {len(hist_revs)}",
            "tags: [小智/记忆]",
            "---",
            "",
            "> [!info] 自动生成，由 anima-brain 同步维护。手动修改会被下次同步覆盖。",
            "",
        ]

        if not active_mems:
            lines.append("目前还没有提取到的长期事实记忆。")
            lines.append("")
        else:
            grouped: dict[str, list[Any]] = {}
            for m in active_mems:
                grouped.setdefault(m.category, []).append(m)

            ordered_cats = [k for k, _ in _CATEGORY_ORDER]
            extra_cats = sorted(c for c in grouped if c not in ordered_cats)
            cat_titles = dict(_CATEGORY_ORDER)

            for cat in [*ordered_cats, *extra_cats]:
                cat_items = grouped.get(cat)
                if not cat_items:
                    continue
                cat_items.sort(key=lambda x: (x.updated_at, x.id), reverse=True)
                lines.append(f"## {cat_titles.get(cat, cat)}")
                for m in cat_items:
                    txt = _single_line_text(m.text)
                    if m.source_turn_id and m.source_turn_id in block_map:
                        d_str = block_map[m.source_turn_id][0]
                    else:
                        d_str = _fmt_date(m.lifecycle.observed_at or m.updated_at, tz)
                    parts = [txt]
                    if d_str:
                        parts.append(d_str)
                    link = self._turn_wikilink(m.source_turn_id, block_map, "原话")
                    if link:
                        parts.append(link)
                    if m.revision > 1:
                        parts.append(f"rev {m.revision}")
                    if m.details.loop_status == "open":
                        parts.append("`待跟进`")
                    lines.append("- " + " · ".join(parts))
                lines.append("")

        lines.append("## 未完待续")
        lines.append(f"- 详见 [[记忆/未完待续|未完待续]]（当前 {open_loops_count} 项待跟进）")
        lines.append("")

        # Bottom Obsidian collapsible callout for historical revisions & superseded facts
        if hist_revs:
            lines.append(f"> [!abstract]- 历史修订与已更正记忆（共 {len(hist_revs)} 条）")
            for item in hist_revs:
                meta_bits = [item["rev_label"]]
                if item["date_str"]:
                    meta_bits.append(item["date_str"])
                cur_suffix = f"；现为：{item['current_text']}" if item["current_text"] else ""
                meta_str = "，".join(meta_bits) + cur_suffix
                link_suffix = f" · {item['link']}" if item["link"] else ""
                lines.append(
                    f"> - ~~{item['old_text']}~~ → {item['reason_zh']}（{meta_str}）{link_suffix}"
                )
        else:
            lines.append("> [!abstract]- 历史修订与已更正记忆（暂无）")
            lines.append("> 暂无已更正或被替代的旧记忆记录。")
        lines.append("")

        return "\n".join(lines), len(active_mems), len(hist_revs)

    def _render_open_loops_note(
        self, snap: dict[str, Any], block_map: dict[str, tuple[str, str]]
    ) -> tuple[str, int]:
        spec = snap["spec"]
        tz = snap["tz"]
        active_mems = [m for m in snap["memories"] if not m.lifecycle.invalidation]
        open_loops = [m for m in active_mems if m.details.loop_status == "open"]
        resolved_loops = [m for m in active_mems if m.details.loop_status == "resolved"]

        open_loops.sort(key=lambda m: (m.details.importance, m.updated_at), reverse=True)
        resolved_loops.sort(key=lambda m: m.updated_at, reverse=True)

        max_ts = max([m.updated_at for m in (*open_loops, *resolved_loops)] + [0.0])

        lines = [
            "---",
            "type: 小智未完待续",
            f"character: {_yaml_scalar(spec.name)}",
            f"updated: {_yaml_optional_updated(max_ts, tz)}",
            f"open_loops: {len(open_loops)}",
            "tags: [小智/记忆, 小智/待办]",
            "---",
            "",
            "> [!info] 自动生成，由 anima-brain 同步维护。手动修改会被下次同步覆盖。",
            "",
            "## 当前待跟进",
        ]
        if not open_loops:
            lines.append("当前没有待跟进的未完待续事项。")
            lines.append("")
        else:
            for m in open_loops:
                txt = _single_line_text(m.text)
                d_str = (
                    block_map[m.source_turn_id][0]
                    if m.source_turn_id and m.source_turn_id in block_map
                    else _fmt_date(m.updated_at, tz)
                )
                bits = [txt]
                if d_str:
                    bits.append(f"记录于 {d_str}")
                if m.details.event_date:
                    bits.append(f"日期：{m.details.event_date.isoformat()}")
                link = self._turn_wikilink(m.source_turn_id, block_map, "原话")
                if link:
                    bits.append(link)
                lines.append("- [ ] " + " · ".join(bits))
            lines.append("")

        if resolved_loops:
            lines.append(f"> [!done]- 已完成事项（共 {len(resolved_loops)} 项）")
            for m in resolved_loops[:30]:
                txt = _single_line_text(m.text)
                d_str = _fmt_date(m.updated_at, tz)
                link = self._turn_wikilink(m.source_turn_id, block_map, "原话")
                suffix = f" · {link}" if link else ""
                lines.append(f"> - [x] {txt} · {d_str}{suffix}")
            lines.append("")

        return "\n".join(lines), len(open_loops)

    def _render_relationship_note(
        self, snap: dict[str, Any], block_map: dict[str, tuple[str, str]]
    ) -> tuple[str, str]:
        spec = snap["spec"]
        state = snap["state"]
        tz = snap["tz"]
        turns = snap["turns"]

        last_turn_ts = turns[-1]["effective_ts"] if turns else 0.0
        state_at = float(state.get("at") or state.get("interaction_at") or 0.0)
        anchor_ts = max(last_turn_ts, state_at)
        eval_ts = anchor_ts if anchor_ts > 0 else time.time()

        decayed_state = emotion.decayed(state, eval_ts, f"{snap['owner']}:{self.character_id}")
        axes_raw = decayed_state.get("axes") or {}
        bands_raw = decayed_state.get("bands") or {}
        pad = decayed_state.get("pad") or [0.0, 0.0, 0.0]

        axes = {
            k: round(float(axes_raw.get(k, 0.0)), 4)
            for k in ("familiarity", "warmth", "trust", "romance", "tension")
        }
        bands = {
            k: max(0, min(3, int(bands_raw.get(k, 0))))
            for k in ("familiarity", "warmth", "trust", "romance", "tension")
        }

        fam_band = bands["familiarity"]
        stage_zh = _STAGE_LABELS_ZH[fam_band]
        if spec.romance_enabled and axes["romance"] >= 0.60 and fam_band >= 2:
            stage_zh = "亲密依恋"
        elif not spec.romance_enabled and fam_band >= 3:
            stage_zh = "挚友信赖"

        cutoff_30d = eval_ts - 30 * 86400
        recent_appraised = [
            t for t in turns if t.get("emotion") and float(t["effective_ts"]) >= cutoff_30d
        ]
        emo_counts = Counter(t["emotion"] for t in recent_appraised)

        def _fmt_pad(v: Any) -> str:
            fv = float(v or 0.0)
            if abs(fv) < 0.005:
                fv = 0.0
            return f"{fv:+.2f}"

        lines = [
            "---",
            "type: 小智关系与情绪状态",
            f"character: {_yaml_scalar(spec.name)}",
            f"updated: {_yaml_optional_updated(anchor_ts, tz)}",
            f"stage: {_yaml_scalar(stage_zh)}",
            f"romance_enabled: {'true' if spec.romance_enabled else 'false'}",
            "tags: [小智/记忆, 小智/情绪]",
            "---",
            "",
            "> [!info] 自动生成，由 anima-brain 同步维护。手动修改会被下次同步覆盖。",
            "",
            "## 当前关系阶段与亲密度四轴",
            f"- 当前阶段：**{stage_zh}**（Lv.{fam_band}）",
            f"- 浪漫模式：**{'已开启' if spec.romance_enabled else '已关闭'}**",
            f"- PAD 情绪坐标：愉悦度 P={_fmt_pad(pad[0])} / 唤醒度 A={_fmt_pad(pad[1])} / 掌控度 D={_fmt_pad(pad[2])}",
            "",
            "| 维度 | 数值 | 等级 | 进度 |",
            "| :--- | ---: | :---: | :--- |",
            f"| 熟悉度 (Familiarity) | {round(axes['familiarity'] * 100)}% | Lv.{bands['familiarity']} | `{_bar(axes['familiarity'])}` |",
            f"| 温暖度 (Warmth) | {round(axes['warmth'] * 100)}% | Lv.{bands['warmth']} | `{_bar(axes['warmth'])}` |",
            f"| 信任度 (Trust) | {round(axes['trust'] * 100)}% | Lv.{bands['trust']} | `{_bar(axes['trust'])}` |",
            f"| 浪漫度 (Romance) | {round(axes['romance'] * 100)}% | {'开启' if spec.romance_enabled else '关闭'} | `{_bar(axes['romance'])}` |",
            f"| 紧张度 (Tension) | {round(axes['tension'] * 100)}% | Lv.{bands['tension']} | `{_bar(axes['tension'])}` |",
            "",
            "## 近 30 天情绪分布",
        ]

        total_30d = len(recent_appraised)
        if total_30d == 0:
            lines.append("近 30 天暂无情绪评估记录。")
            lines.append("")
        else:
            lines.append(f"近 30 天共记录 **{total_30d}** 轮有效情绪评估：")
            lines.append("")
            lines.append("| 情绪 | 次数 | 占比 | 分布 |")
            lines.append("| :--- | ---: | ---: | :--- |")
            for emo_key, cnt in emo_counts.most_common():
                ratio = cnt / total_30d
                lines.append(
                    f"| {_EMOTION_ZH.get(emo_key, emo_key)} | {cnt} | {ratio * 100:.1f}% | `{_bar(ratio)}` |"
                )
            lines.append("")
            lines.append("### 最近情绪片段")
            for t in reversed(recent_appraised[-10:]):
                link = self._turn_wikilink(t["id"], block_map, f"{t['date']} {t['time']}")
                raw_snip = clean_dialogue_text(t["user_message"], escape_block_breakers=False).replace("\n", " ")
                snippet = raw_snip[:42] + ("…" if len(raw_snip) > 42 else "")
                lines.append(
                    f"- {link} · **{_EMOTION_ZH.get(t['emotion'], t['emotion'])}**（强度 {t['intensity']}）：{snippet}"
                )
            lines.append("")

        return "\n".join(lines), stage_zh

    def _render_core_note(
        self, snap: dict[str, Any], block_map: dict[str, tuple[str, str]]
    ) -> tuple[str, int]:
        spec = snap["spec"]
        tz = snap["tz"]
        core_docs = [d for d in snap["documents"] if d["kind"] in ("core", "character_core")]
        max_ts = max([d["created_at"] for d in core_docs] + [0.0])

        lines = [
            "---",
            "type: 小智核心人物笔记",
            f"character: {_yaml_scalar(spec.name)}",
            f"updated: {_yaml_optional_updated(max_ts, tz)}",
            f"documents: {len(core_docs)}",
            "tags: [小智/记忆]",
            "---",
            "",
            "> [!info] 自动生成，由 anima-brain 后台整理维护。手动修改会被下次同步覆盖。",
            "",
        ]
        if not core_docs:
            lines.append(
                "当前尚未生成核心人物综合笔记。随着对话积累，Anima Brain 会在后台记忆整理（maintenance）时自动提炼并同步到这里。"
            )
            lines.append("")
        else:
            for idx, doc in enumerate(core_docs, 1):
                c_date = _fmt_iso_min(doc["created_at"], tz)
                title = f"## 核心人物档案 #{idx}" if len(core_docs) > 1 else "## 核心人物档案"
                if c_date:
                    title += f"（{c_date}）"
                lines.append(title)
                lines.append("")
                lines.append(redact_credentials(doc["text"]).strip())
                lines.append("")
                src_links = self._source_ids_to_links(doc.get("sources") or [], block_map)
                if src_links:
                    lines.append("关联原话：" + " · ".join(src_links[:12]))
                    lines.append("")

        return "\n".join(lines), len(core_docs)

    def _source_ids_to_links(
        self, source_ids: list[str], block_map: dict[str, tuple[str, str]]
    ) -> list[str]:
        links: list[str] = []
        seen_turns: set[str] = set()
        for sid in source_ids:
            tid = str(sid).split(":", 1)[0]
            if not tid or tid in seen_turns or tid not in block_map:
                continue
            seen_turns.add(tid)
            d_str, blk = block_map[tid]
            links.append(f"[[{_dialogue_rel_path(d_str)}#^{blk}|{d_str} #{blk[2:6]}]]")
        return links

    def _render_episodes(
        self, snap: dict[str, Any], block_map: dict[str, tuple[str, str]]
    ) -> list[tuple[str, str, str, str]]:
        """Return list of (filename, date_str, summary_preview, markdown_content) for episode docs."""
        spec = snap["spec"]
        tz = snap["tz"]
        ep_docs = [d for d in snap["documents"] if d["kind"] == "episode"]
        rendered: list[tuple[str, str, str, str]] = []

        for doc in ep_docs:
            meta = doc.get("meta") or {}
            covers_from = meta.get("covers_from")
            covers_to = meta.get("covers_to")
            anchor_ts = float(covers_from or doc["created_at"] or 0.0)
            d_str = _fmt_date(anchor_ts, tz) or _fmt_date(doc["created_at"], tz) or "unknown"
            doc_short = re.sub(r"[^a-zA-Z0-9]", "", doc["id"]).lower()[:8]
            filename = f"{d_str}-片段-{doc_short}.md"

            text_clean = redact_credentials(doc["text"]).strip()
            importance = int(meta.get("importance") or 5)
            unresolved = meta.get("unresolved") or []
            src_links = self._source_ids_to_links(doc.get("sources") or [], block_map)

            lines = [
                "---",
                "type: 小智互动片段",
                f"date: {d_str}",
                f"character: {_yaml_scalar(spec.name)}",
                f"document_id: {_yaml_scalar(doc['id'])}",
                f"importance: {importance}",
                "tags: [小智/记忆, 小智/片段]",
                "---",
                "",
                "> [!info] 自动生成，由 anima-brain 记忆整理提炼。",
                "",
                f"# {d_str} 互动片段",
                "",
                text_clean,
                "",
            ]
            if covers_from and covers_to:
                lines.append(
                    f"- 覆盖时段：{_fmt_iso_sec(covers_from, tz)} ～ {_fmt_iso_sec(covers_to, tz)}"
                )
            lines.append(f"- 提炼时间：{_fmt_iso_sec(doc['created_at'], tz)}")
            if unresolved:
                lines.append("")
                lines.append("## 片段内提及的待续线索")
                for u in unresolved:
                    lines.append(f"- {_single_line_text(str(u))}")
            if src_links:
                lines.append("")
                lines.append("## 原话溯源")
                for lk in src_links:
                    lines.append(f"- {lk}")
            lines.append("")

            flat_txt = text_clean.replace("\n", " ")
            preview = flat_txt[:48] + ("…" if len(flat_txt) > 48 else "")
            rendered.append((filename, d_str, preview, "\n".join(lines)))

        rendered.sort(key=lambda x: (x[1], x[0]), reverse=True)
        return rendered

    def _render_overview(
        self,
        snap: dict[str, Any],
        by_date: dict[str, list[dict[str, Any]]],
        *,
        active_facts: int,
        historical_revisions: int,
        open_loops: int,
        stage_zh: str,
        core_count: int,
        episodes: list[tuple[str, str, str, str]],
    ) -> str:
        spec = snap["spec"]
        tz = snap["tz"]
        turns = snap["turns"]
        max_ts = max(
            [t["effective_ts"] for t in turns]
            + [m.updated_at for m in snap["memories"]]
            + [float(r.get("changed_at") or 0.0) for r in snap["revisions"]]
            + [d["created_at"] for d in snap["documents"]]
            + [float((snap["state"] or {}).get("at") or 0.0)]
            + [0.0]
        )
        sorted_dates = sorted(by_date.keys(), reverse=True)

        lines = [
            "---",
            "type: 小智记忆总览",
            f"character: {_yaml_scalar(spec.name)}",
            f"updated: {_yaml_optional_updated(max_ts, tz)}",
            f"turns: {len(turns)}",
            f"days: {len(sorted_dates)}",
            f"facts: {active_facts}",
            f"open_loops: {open_loops}",
            f"episodes: {len(episodes)}",
            "tags: [小智/总览]",
            "---",
            "",
            f"# {spec.name} · 记忆与对话库",
            "",
            "> [!info] 由 Anima Brain 单向自动同步生成。每日对话按日归档，长期记忆与片段均支持一键跳回原话出处。",
            "",
            "## 记忆入口",
            f"- [[记忆/芽衣记得的事|芽衣记得的事]]：当前有效记忆 **{active_facts}** 条（文末折叠区保留 {historical_revisions} 条历史修订）",
            f"- [[记忆/未完待续|未完待续]]：待跟进事项 **{open_loops}** 项",
            f"- [[记忆/关系与情绪状态|关系与情绪状态]]：当前关系阶段 **{stage_zh}**",
            f"- [[记忆/核心人物笔记|核心人物笔记]]：综合档案 **{core_count}** 份",
            "- [[简报/最新简报|离线每日/每周简报]]：低成本离线待跟进梳理与近期摘要（无主动语音打扰）",
            "",
            f"## 每日对话归档（共 {len(sorted_dates)} 天 · {len(turns)} 轮）",
        ]
        if not sorted_dates:
            lines.append("暂无已提交对话记录。")
        else:
            for d_str in sorted_dates[:30]:
                day_turns = by_date[d_str]
                top_moods = [
                    m for m, _ in Counter(t["emotion"] for t in day_turns if t.get("emotion")).most_common(3)
                ]
                mood_txt = f" · 情绪：{' / '.join(top_moods)}" if top_moods else ""
                rel = _dialogue_rel_path(d_str)
                lines.append(f"- [[{rel}|{d_str}]] · {len(day_turns)} 轮{mood_txt}")
            if len(sorted_dates) > 30:
                lines.append(f"- ……其余 {len(sorted_dates) - 30} 天见 `对话/` 目录")
        lines.append("")

        lines.append(f"## 最近互动片段（共 {len(episodes)} 篇）")
        if not episodes:
            lines.append("暂无互动片段摘要。")
        else:
            for fname, d_str, preview, _ in episodes[:12]:
                stem = fname[:-3]
                lines.append(f"- [[记忆/互动片段/{stem}|{d_str} 互动片段]]：{preview}")
        lines.append("")

        return "\n".join(lines)

    def _run_export(
        self,
        *,
        mode: str,
        dirty_dates: set[str] | None = None,
        reason: str = "",
        force_all_dates: bool = False,
        include_snapshots: bool = True,
    ) -> dict[str, Any]:
        """Core export executor. Never raises unhandled I/O exceptions; returns structured report."""
        started = time.monotonic()
        with self._op_lock:
            writable, probe_err = _probe_writable(self.export_dir)
            self._writable = writable
            if not writable:
                self._last_error = f"Vault 目录不可写: {probe_err}"
                log.warning("Obsidian export skipped (%s): %s", reason or mode, self._last_error)
                return {
                    "ok": False,
                    "error": self._last_error,
                    "writable": False,
                    "written": 0,
                    "skipped": 0,
                }

            try:
                snap = self._read_snapshot_data()
            except Exception as exc:
                self._last_error = f"读取记忆库失败: {redact_credentials(str(exc))[:160]}"
                log.warning("Obsidian export read failed: %s", self._last_error)
                return {
                    "ok": False,
                    "error": self._last_error,
                    "writable": True,
                    "written": 0,
                    "skipped": 0,
                }

            tz = snap["tz"]
            spec = snap["spec"]
            fp, turn_sigs = self._compute_fingerprint_from_snapshot(snap)
            if mode == "incremental_if_changed":
                extra_only = set(dirty_dates or ())
                if fp and fp == self._last_fingerprint and not extra_only:
                    self._last_export_seq = snap.get("export_seq")
                    return {"ok": True, "changed": False, "written": 0, "skipped": 0}
                computed_dirty: set[str] = set(extra_only)
                today_s = datetime.now(tz).strftime("%Y-%m-%d")
                if not self._known_turn_sigs:
                    for d_str in {sig[0] for sig in turn_sigs.values()}:
                        target = self.export_dir / (_dialogue_rel_path(d_str) + ".md")
                        if d_str >= today_s or not target.is_file():
                            computed_dirty.add(d_str)
                else:
                    for tid, sig in turn_sigs.items():
                        old_sig = self._known_turn_sigs.get(tid)
                        if old_sig != sig:
                            computed_dirty.add(sig[0])
                            if old_sig is not None and old_sig[0] != sig[0]:
                                computed_dirty.add(old_sig[0])
                    for tid, old_sig in self._known_turn_sigs.items():
                        if tid not in turn_sigs:
                            computed_dirty.add(old_sig[0])
                computed_dirty.add(today_s)
                dirty_dates = computed_dirty

            by_date, block_map = self._build_turn_index(snap["turns"])
            today_str = datetime.now(tz).strftime("%Y-%m-%d")

            written = 0
            skipped = 0

            try:
                # 1. Determine which dialogue date files to write according to frozen-day policy
                dates_to_write: list[str] = []
                if force_all_dates:
                    dates_to_write = sorted(by_date.keys())
                elif mode == "startup_backfill":
                    for d_str in sorted(by_date.keys()):
                        target = self.export_dir / (_dialogue_rel_path(d_str) + ".md")
                        if d_str >= today_str or not target.is_file():
                            dates_to_write.append(d_str)
                else:
                    # Incremental: ONLY dates explicitly dirty (today or delayed turn date)
                    target_set = set(dirty_dates or ())
                    for d_str in sorted(target_set):
                        if d_str in by_date:
                            dates_to_write.append(d_str)

                for d_str in dates_to_write:
                    target = self.export_dir / (_dialogue_rel_path(d_str) + ".md")
                    md = self._render_daily_dialogue(d_str, by_date[d_str], spec)
                    res = _atomic_write_if_changed(target, md)
                    if res == "written":
                        written += 1
                    else:
                        skipped += 1

                # 2. Memory snapshots + overview
                active_facts = sum(1 for m in snap["memories"] if not m.lifecycle.invalidation)
                hist_revs = len(self._collect_historical_revisions(snap, block_map))
                open_loops = sum(
                    1
                    for m in snap["memories"]
                    if not m.lifecycle.invalidation and m.details.loop_status == "open"
                )
                episodes_rendered = self._render_episodes(snap, block_map)

                if include_snapshots:
                    mem_dir = self.export_dir / "记忆"
                    ep_dir = mem_dir / "互动片段"

                    mem_md, active_facts, hist_revs = self._render_memories_note(snap, block_map)
                    loops_md, open_loops = self._render_open_loops_note(snap, block_map)
                    rel_md, stage_zh = self._render_relationship_note(snap, block_map)
                    core_md, core_count = self._render_core_note(snap, block_map)
                    overview_md = self._render_overview(
                        snap,
                        by_date,
                        active_facts=active_facts,
                        historical_revisions=hist_revs,
                        open_loops=open_loops,
                        stage_zh=stage_zh,
                        core_count=core_count,
                        episodes=episodes_rendered,
                    )

                    snapshot_files = [
                        (self.export_dir / "总览.md", overview_md),
                        (mem_dir / "芽衣记得的事.md", mem_md),
                        (mem_dir / "未完待续.md", loops_md),
                        (mem_dir / "关系与情绪状态.md", rel_md),
                        (mem_dir / "核心人物笔记.md", core_md),
                    ]
                    for target, content in snapshot_files:
                        res = _atomic_write_if_changed(target, content)
                        if res == "written":
                            written += 1
                        else:
                            skipped += 1

                    # Episode notes
                    ep_dir.mkdir(parents=True, exist_ok=True)
                    active_ep_names = set()
                    for fname, _, _, ep_md in episodes_rendered:
                        active_ep_names.add(fname)
                        res = _atomic_write_if_changed(ep_dir / fname, ep_md)
                        if res == "written":
                            written += 1
                        else:
                            skipped += 1

                    # Prune only stale generated episode files whose source document was deleted
                    for existing_file in ep_dir.iterdir():
                        if (
                            existing_file.is_file()
                            and _GENERATED_EPISODE_FILE_RE.match(existing_file.name)
                            and existing_file.name not in active_ep_names
                        ):
                            try:
                                existing_file.unlink()
                            except OSError:
                                pass

                # Update tracking state & fingerprint directly from in-memory snapshot (zero duplicate DB scan)
                self._last_fingerprint = fp
                self._known_turn_sigs = turn_sigs
                self._last_export_seq = snap.get("export_seq")
                self._last_error = ""
                now = time.time()
                self._last_export_ts = now
                self._last_export_at = _fmt_iso_sec(now, tz)
                self._last_export_reason = reason or mode
                self._last_stats = {
                    "turns": len(snap["turns"]),
                    "days": len(by_date),
                    "active_facts": active_facts,
                    "historical_revisions": hist_revs,
                    "open_loops": open_loops,
                    "episodes": len(episodes_rendered),
                    "files_written": written,
                    "files_skipped": skipped,
                }
                dur_ms = round((time.monotonic() - started) * 1000, 1)
                log.info(
                    "Obsidian export (%s) completed in %.1fms: written=%d skipped=%d turns=%d days=%d facts=%d",
                    self._last_export_reason,
                    dur_ms,
                    written,
                    skipped,
                    len(snap["turns"]),
                    len(by_date),
                    active_facts,
                )
                return {
                    "ok": True,
                    "mode": mode,
                    "reason": self._last_export_reason,
                    "writable": True,
                    "written": written,
                    "skipped": skipped,
                    "duration_ms": dur_ms,
                    "last_export_at": self._last_export_at,
                    "stats": dict(self._last_stats),
                }
            except OSError as exc:
                self._writable = False
                self._last_error = f"写入 Obsidian 目录失败: {redact_credentials(str(exc))[:180]}"
                log.warning("Obsidian export write error: %s", self._last_error)
                return {
                    "ok": False,
                    "error": self._last_error,
                    "writable": False,
                    "written": written,
                    "skipped": skipped,
                }
            except Exception as exc:
                self._last_error = f"导出异常: {redact_credentials(str(exc))[:180]}"
                log.warning("Obsidian export unexpected error: %s", self._last_error)
                return {
                    "ok": False,
                    "error": self._last_error,
                    "writable": bool(self._writable),
                    "written": written,
                    "skipped": skipped,
                }

    def sync_startup_backfill(self) -> dict[str, Any]:
        """Startup run: backfill missing historical days + sync today + snapshots (never alters existing past days)."""
        return self._run_export(
            mode="startup_backfill",
            reason="startup_backfill",
            force_all_dates=False,
            include_snapshots=True,
        )

    def sync_incremental(
        self, dirty_dates: set[str] | None = None, *, include_snapshots: bool = True, reason: str = "incremental"
    ) -> dict[str, Any]:
        """Incremental run: update ONLY dirty_dates (today or delayed turn date) + snapshots."""
        return self._run_export(
            mode="incremental",
            dirty_dates=dirty_dates,
            reason=reason,
            force_all_dates=False,
            include_snapshots=include_snapshots,
        )

    def sync_if_db_changed(
        self,
        *,
        reason: str = "snapshot_poll",
        extra_dirty_dates: set[str] | None = None,
    ) -> dict[str, Any]:
        """Periodic/post-Studio check: O(1) sequence short-circuit + single-snapshot incremental export (P1-5)."""
        with self._op_lock:
            extra = set(extra_dirty_dates or ())
            if self._last_fingerprint and not extra and self._last_export_seq is not None:
                cur_seq = self._read_export_seq()
                if cur_seq is not None and cur_seq == self._last_export_seq:
                    return {"ok": True, "changed": False, "written": 0, "skipped": 0}

            res = self._run_export(
                mode="incremental_if_changed",
                dirty_dates=extra,
                reason=reason,
                force_all_dates=False,
                include_snapshots=True,
            )
            if res.get("changed") is False:
                return res
            res["changed"] = True
            return res

    def rebuild_all(self, *, reason: str = "manual_rebuild") -> dict[str, Any]:
        """Manual full rebuild: re-render ALL dates (including past days) + snapshots; skips identical hashes."""
        return self._run_export(
            mode="full_rebuild",
            reason=reason,
            force_all_dates=True,
            include_snapshots=True,
        )

    def resolve_turn_date(self, occurred_at: float | None = None) -> str:
        try:
            engine = self.brain.engine
            with engine._lock:
                row = engine.db.execute(
                    "SELECT spec FROM characters WHERE owner=? AND id=?",
                    (engine.owner, self.character_id),
                ).fetchone()
                tz = _resolve_tz(CharacterSpec.model_validate_json(row["spec"]).timezone if row else "Asia/Shanghai")
        except Exception:
            tz = _resolve_tz("Asia/Shanghai")
        ts = float(occurred_at) if occurred_at and float(occurred_at) > 0 else time.time()
        return _fmt_date(ts, tz)

    @staticmethod
    def _briefing_safe_text(text: str, max_len: int = 160) -> str:
        s = _single_line_text(redact_credentials(str(text or "")))
        if _recall_sanitizer is not None:
            try:
                s, _ = _recall_sanitizer.redact_sensitive_entities(s)
            except Exception:
                pass
        if len(s) > max_len:
            s = s[: max_len - 1].rstrip() + "…"
        return s

    def _classify_open_loops(self, snap: dict[str, Any], target_day) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
        """Partition open follow-ups into urgent/near-term vs regular, plus recently resolved items."""
        active_mems = [m for m in snap["memories"] if not m.lifecycle.invalidation]
        open_mems = [m for m in active_mems if m.details.loop_status == "open"]
        resolved_mems = [m for m in active_mems if m.details.loop_status == "resolved"]
        urgent: list[dict[str, Any]] = []
        regular: list[dict[str, Any]] = []
        for m in open_mems:
            ev_date = m.details.event_date
            days_left: int | None = None
            is_urgent = False
            if ev_date is not None:
                days_left = (ev_date - target_day).days
                if days_left <= 3:
                    is_urgent = True
            if int(m.details.importance or 5) >= 8:
                is_urgent = True
            rec = {"memory": m, "days_left": days_left}
            if is_urgent:
                urgent.append(rec)
            else:
                regular.append(rec)
        urgent.sort(
            key=lambda x: (
                x["days_left"] if x["days_left"] is not None else 999,
                -int(x["memory"].details.importance or 5),
                -float(x["memory"].updated_at or 0.0),
            )
        )
        regular.sort(
            key=lambda x: (
                -int(x["memory"].details.importance or 5),
                -float(x["memory"].updated_at or 0.0),
            )
        )
        return urgent, regular, resolved_mems

    @staticmethod
    def _memory_effective_date(
        memory: Any, block_map: dict[str, tuple[str, str]], tz: ZoneInfo
    ) -> str:
        tid = getattr(memory, "source_turn_id", None)
        if tid and tid in block_map:
            return block_map[tid][0]
        return _fmt_date(float(getattr(memory, "updated_at", 0.0) or 0.0), tz)

    def _render_daily_briefing(
        self,
        snap: dict[str, Any],
        by_date: dict[str, list[dict[str, Any]]],
        block_map: dict[str, tuple[str, str]],
        target_dt: datetime,
    ) -> tuple[str, dict[str, Any]]:
        tz = snap["tz"]
        spec = snap["spec"]
        day_str = target_dt.strftime("%Y-%m-%d")
        day_date = target_dt.date()
        day_turns = by_date.get(day_str, [])
        urgent, regular, resolved_all = self._classify_open_loops(snap, day_date)
        resolved_today = [
            m for m in resolved_all if self._memory_effective_date(m, block_map, tz) == day_str
        ]
        new_facts_today = [
            m
            for m in snap["memories"]
            if not m.lifecycle.invalidation
            and self._memory_effective_date(m, block_map, tz) == day_str
        ]
        day_episodes = [
            d
            for d in snap["documents"]
            if d.get("kind") == "episode"
            and _fmt_date(float(d.get("created_at") or 0.0), tz) == day_str
        ]

        src_counts = Counter(t.get("source") or "esp32" for t in day_turns)
        mood_counts = Counter(t["emotion"] for t in day_turns if t.get("emotion"))
        max_ts = max(
            [float(t.get("effective_ts") or 0.0) for t in day_turns]
            + [float(m.updated_at or 0.0) for m in new_facts_today]
            + [float(x["memory"].updated_at or 0.0) for x in (*urgent, *regular)]
            + [0.0]
        )

        lines = [
            "---",
            "type: 小智每日简报",
            "period: daily",
            f"date: {day_str}",
            f"character: {_yaml_scalar(spec.name)}",
            f"updated: {_yaml_optional_updated(max_ts, tz)}",
            f"turns: {len(day_turns)}",
            f"open_loops: {len(urgent) + len(regular)}",
            f"urgent_loops: {len(urgent)}",
            f"new_facts: {len(new_facts_today)}",
            "tags: [小智/简报/每日]",
            "---",
            "",
            f"# {day_str} · 离线每日待跟进与摘要简报",
            "",
            "> [!note] 离线低成本自动梳理生成（仅写入 Obsidian，不触发主动语音打扰）。",
            "",
            "## 一、待跟进事项分层",
        ]
        if not urgent and not regular:
            lines.append("- 当前无未完待续事项。")
        else:
            if urgent:
                lines.append("### 优先 / 临近待跟进")
                for item in urgent[:10]:
                    m = item["memory"]
                    dl = item["days_left"]
                    due_txt = ""
                    if dl is not None:
                        if dl < 0:
                            due_txt = f" · 已过目标日 {-dl} 天"
                        elif dl == 0:
                            due_txt = " · 今天"
                        else:
                            due_txt = f" · 距目标日 {dl} 天"
                    link = self._turn_wikilink(m.source_turn_id, block_map, "原话")
                    suffix = f" ({link})" if link else ""
                    lines.append(
                        f"- [ ] **{self._briefing_safe_text(m.text, 140)}** "
                        f"（重要度 {int(m.details.importance or 5)}/10{due_txt}）{suffix}"
                    )
            if regular:
                lines.append("### 常规待跟进")
                for item in regular[:12]:
                    m = item["memory"]
                    rec_d = self._memory_effective_date(m, block_map, tz)
                    link = self._turn_wikilink(m.source_turn_id, block_map, "原话")
                    suffix = f" ({link})" if link else ""
                    lines.append(
                        f"- [ ] {self._briefing_safe_text(m.text, 140)}（记录于 {rec_d}）{suffix}"
                    )
        if resolved_today:
            lines.append("### 今日已完结")
            for m in resolved_today[:8]:
                lines.append(f"- [x] {self._briefing_safe_text(m.text, 140)}")
        lines.append("")

        lines.append("## 二、今日互动与情绪脉络")
        if not day_turns:
            lines.append(f"- {day_str} 暂无已提交对话记录。")
        else:
            rel = _dialogue_rel_path(day_str)
            src_desc = " / ".join(
                f"{'ESP32' if k == 'esp32' else 'Studio'} {v} 轮" for k, v in src_counts.most_common()
            )
            mood_desc = (
                " / ".join(f"{k}({v})" for k, v in mood_counts.most_common(4))
                if mood_counts
                else "平稳"
            )
            lines.append(f"- 共 **{len(day_turns)}** 轮互动（{src_desc}）· 情绪脉络：{mood_desc} · [[{rel}|查看当日完整对话]]")
            for t in day_turns[-6:]:
                hhmm = _fmt_time(t["effective_ts"], tz)
                u_prev = self._briefing_safe_text(t.get("user_message") or "", 90)
                bid = f"t-{t['id'].replace('-', '')[:8]}"
                lines.append(f"  - `{hhmm}` {u_prev}（[[{rel}#^{bid}|跳转]]）")
        lines.append("")

        lines.append("## 三、今日沉淀记忆与片段")
        if not new_facts_today and not day_episodes:
            lines.append("- 今日暂无新增或更新的长期记忆条目。")
        else:
            for m in new_facts_today[:12]:
                lines.append(f"- `[{m.category}]` {self._briefing_safe_text(m.text, 150)}")
            for ep in day_episodes[:4]:
                lines.append(f"- **互动片段**：{self._briefing_safe_text(ep.get('text') or '', 200)}")
        lines.append("")

        return "\n".join(lines), {
            "daily_date": day_str,
            "daily_turns": len(day_turns),
            "open_loops": len(urgent) + len(regular),
            "urgent_loops": len(urgent),
        }

    def _render_weekly_briefing(
        self,
        snap: dict[str, Any],
        by_date: dict[str, list[dict[str, Any]]],
        block_map: dict[str, tuple[str, str]],
        target_dt: datetime,
    ) -> tuple[str, dict[str, Any]]:
        tz = snap["tz"]
        spec = snap["spec"]
        iso_year, iso_week, iso_weekday = target_dt.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        week_start_date = (target_dt - timedelta(days=iso_weekday - 1)).date()
        week_end_date = week_start_date + timedelta(days=6)
        week_dates = [(week_start_date + timedelta(days=i)).isoformat() for i in range(7)]

        week_turns: list[dict[str, Any]] = []
        active_days: list[str] = []
        for d_str in week_dates:
            dts = by_date.get(d_str, [])
            if dts:
                active_days.append(d_str)
                week_turns.extend(dts)

        week_date_set = set(week_dates)
        week_facts = [
            m
            for m in snap["memories"]
            if not m.lifecycle.invalidation
            and self._memory_effective_date(m, block_map, tz) in week_date_set
        ]
        week_episodes = [
            d
            for d in snap["documents"]
            if d.get("kind") == "episode"
            and _fmt_date(float(d.get("created_at") or 0.0), tz) in week_date_set
        ]
        core_docs = [d for d in snap["documents"] if d.get("kind") in ("core", "character_core")]

        urgent, regular, resolved_all = self._classify_open_loops(snap, target_dt.date())
        resolved_week = [
            m for m in resolved_all if self._memory_effective_date(m, block_map, tz) in week_date_set
        ]
        carry_over = [
            item
            for item in (*urgent, *regular)
            if self._memory_effective_date(item["memory"], block_map, tz) < week_start_date.isoformat()
        ]

        max_ts = max(
            [float(t.get("effective_ts") or 0.0) for t in week_turns]
            + [float(m.updated_at or 0.0) for m in week_facts]
            + [float(x["memory"].updated_at or 0.0) for x in (*urgent, *regular)]
            + [0.0]
        )

        lines = [
            "---",
            "type: 小智每周简报",
            "period: weekly",
            f"week: {week_key}",
            f"week_start: {week_start_date.isoformat()}",
            f"week_end: {week_end_date.isoformat()}",
            f"character: {_yaml_scalar(spec.name)}",
            f"updated: {_yaml_optional_updated(max_ts, tz)}",
            f"turns: {len(week_turns)}",
            f"active_days: {len(active_days)}",
            f"open_loops: {len(urgent) + len(regular)}",
            f"episodes: {len(week_episodes)}",
            "tags: [小智/简报/每周]",
            "---",
            "",
            f"# {week_key}（{week_start_date.isoformat()} ～ {week_end_date.isoformat()}）· 每周离线梳理简报",
            "",
            "> [!note] 按周离线聚合待跟进事项、活跃互动与记忆沉淀，方便统一回看，无主动语音打扰。",
            "",
            "## 一、本周待跟进总览",
            f"- 当前未完待续：**{len(urgent) + len(regular)}** 项（优先 **{len(urgent)}** 项 · 跨周遗留 **{len(carry_over)}** 项 · 本周已完结 **{len(resolved_week)}** 项）",
        ]
        for item in (*urgent[:8], *regular[:8]):
            m = item["memory"]
            rec_d = self._memory_effective_date(m, block_map, tz)
            tag = "跨周未闭环" if rec_d < week_start_date.isoformat() else "本周新增"
            link = self._turn_wikilink(m.source_turn_id, block_map, "原话")
            suffix = f" ({link})" if link else ""
            lines.append(f"- [ ] `[{tag}]` {self._briefing_safe_text(m.text, 140)}（{rec_d}）{suffix}")
        lines.append("")

        lines.append("## 二、本周互动日历")
        if not active_days:
            lines.append("- 本周暂无已提交对话。")
        else:
            for d_str in reversed(active_days):
                dts = by_date[d_str]
                moods = [m for m, _ in Counter(t["emotion"] for t in dts if t.get("emotion")).most_common(3)]
                mood_txt = f" · 情绪：{' / '.join(moods)}" if moods else ""
                rel = _dialogue_rel_path(d_str)
                lines.append(
                    f"- [[{rel}|{d_str}]] · {len(dts)} 轮{mood_txt} · [[简报/每日/{d_str}|当日简报]]"
                )
        lines.append("")

        lines.append("## 三、本周记忆与片段沉淀")
        cat_counts = Counter(m.category for m in week_facts)
        if cat_counts:
            cat_desc = " / ".join(f"{k}: {v}" for k, v in cat_counts.most_common())
            lines.append(f"- 本周新增/更新记忆 **{len(week_facts)}** 条（{cat_desc}）")
            for m in week_facts[:12]:
                lines.append(f"  - `[{m.category}]` {self._briefing_safe_text(m.text, 140)}")
        else:
            lines.append("- 本周暂无新增/更新长期记忆条目。")
        if week_episodes:
            lines.append("### 本周互动片段摘要")
            for ep in week_episodes[:6]:
                lines.append(f"- {self._briefing_safe_text(ep.get('text') or '', 220)}")
        if core_docs:
            lines.append("### 核心档案要点")
            for cd in core_docs[:2]:
                lines.append(f"- {self._briefing_safe_text(cd.get('text') or '', 220)}")
        lines.append("")

        return "\n".join(lines), {
            "week_key": week_key,
            "weekly_turns": len(week_turns),
            "active_days": len(active_days),
        }

    def _render_latest_briefing_index(
        self,
        spec: Any,
        daily_meta: dict[str, Any],
        weekly_meta: dict[str, Any],
        snap: dict[str, Any],
        block_map: dict[str, tuple[str, str]],
        target_dt: datetime,
    ) -> str:
        tz = snap["tz"]
        day_str = daily_meta["daily_date"]
        week_key = weekly_meta["week_key"]
        urgent, regular, _ = self._classify_open_loops(snap, target_dt.date())
        lines = [
            "---",
            "type: 小智简报索引",
            f"character: {_yaml_scalar(spec.name)}",
            f"latest_daily: {day_str}",
            f"latest_weekly: {week_key}",
            f"open_loops: {len(urgent) + len(regular)}",
            "tags: [小智/简报]",
            "---",
            "",
            f"# {spec.name} · 离线每日/每周简报入口",
            "",
            f"- 最新每日简报：[[简报/每日/{day_str}|{day_str} 每日简报]]（当日 {daily_meta['daily_turns']} 轮）",
            f"- 最新每周简报：[[简报/每周/{week_key}|{week_key} 每周简报]]（本周 {weekly_meta['weekly_turns']} 轮 · 活跃 {weekly_meta['active_days']} 天）",
            f"- 全部待跟进：[[记忆/未完待续|未完待续]]（共 {len(urgent) + len(regular)} 项 · 优先 {len(urgent)} 项）",
            "",
            "## 置顶待跟进速览",
        ]
        top_items = [*urgent[:5], *regular[:5]][:6]
        if not top_items:
            lines.append("- 当前无未完待续事项。")
        else:
            for item in top_items:
                m = item["memory"]
                rec_d = self._memory_effective_date(m, block_map, tz)
                lines.append(f"- [ ] {self._briefing_safe_text(m.text, 140)}（{rec_d}）")
        lines.append("")
        return "\n".join(lines)

    def export_briefings(
        self,
        *,
        reason: str = "scheduled_briefing",
        target_date: str | None = None,
    ) -> dict[str, Any]:
        """Generate low-cost offline layered daily & weekly briefings in Obsidian (no voice push)."""
        started = time.monotonic()
        with self._op_lock:
            writable, probe_err = _probe_writable(self.export_dir)
            self._writable = writable
            if not writable:
                err = f"Vault 目录不可写: {probe_err}"
                return {"ok": False, "error": err, "writable": False, "written": 0, "skipped": 0}
            try:
                snap = self._read_snapshot_data()
            except Exception as exc:
                err = f"读取记忆库失败: {redact_credentials(str(exc))[:160]}"
                return {"ok": False, "error": err, "writable": True, "written": 0, "skipped": 0}

            tz = snap["tz"]
            spec = snap["spec"]
            if target_date is not None and str(target_date).strip():
                raw_td = str(target_date).strip()
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_td):
                    raise ValueError(f"Invalid target_date format (expected YYYY-MM-DD): {raw_td}")
                try:
                    parsed_d = datetime.strptime(raw_td, "%Y-%m-%d").date()
                except ValueError as exc:
                    raise ValueError(f"Invalid calendar target_date: {raw_td}") from exc
                target_dt = datetime(parsed_d.year, parsed_d.month, parsed_d.day, 12, 0, 0, tzinfo=tz)
            else:
                target_dt = datetime.now(tz)

            by_date, block_map = self._build_turn_index(snap["turns"])
            daily_md, daily_meta = self._render_daily_briefing(snap, by_date, block_map, target_dt)
            weekly_md, weekly_meta = self._render_weekly_briefing(snap, by_date, block_map, target_dt)
            index_md = self._render_latest_briefing_index(
                spec, daily_meta, weekly_meta, snap, block_map, target_dt
            )

            brief_root = self.export_dir / "简报"
            index_path = brief_root / "最新简报.md"
            write_latest_index = True
            if target_date is not None and str(target_date).strip() and index_path.is_file():
                try:
                    existing_idx = index_path.read_text(encoding="utf-8", errors="replace")
                    m_prev = re.search(r"(?m)^latest_daily:\s*(\d{4}-\d{2}-\d{2})\s*$", existing_idx)
                    if m_prev and m_prev.group(1) > daily_meta["daily_date"]:
                        write_latest_index = False
                except OSError:
                    pass

            targets = [
                (brief_root / "每日" / f"{daily_meta['daily_date']}.md", daily_md),
                (brief_root / "每周" / f"{weekly_meta['week_key']}.md", weekly_md),
            ]
            if write_latest_index:
                targets.append((index_path, index_md))
            written = 0
            skipped = 0
            try:
                for path, content in targets:
                    res = _atomic_write_if_changed(path, content)
                    if res == "written":
                        written += 1
                    else:
                        skipped += 1
            except OSError as exc:
                self._writable = False
                err = f"写入简报失败: {redact_credentials(str(exc))[:180]}"
                return {"ok": False, "error": err, "writable": False, "written": written, "skipped": skipped}

            now = time.time()
            self._last_briefing_ts = now
            self._last_briefing_at = _fmt_iso_sec(now, tz)
            self._last_briefing_reason = reason
            self._last_briefing_daily_date = daily_meta["daily_date"]
            self._last_briefing_week_key = weekly_meta["week_key"]
            self._last_briefing_stats = {
                "written": written,
                "skipped": skipped,
                "daily_turns": daily_meta["daily_turns"],
                "weekly_turns": weekly_meta["weekly_turns"],
                "open_loops": daily_meta["open_loops"],
                "urgent_loops": daily_meta["urgent_loops"],
            }
            dur_ms = round((time.monotonic() - started) * 1000, 1)
            log.info(
                "Obsidian offline briefing (%s) completed in %.1fms: day=%s week=%s written=%d skipped=%d",
                reason,
                dur_ms,
                daily_meta["daily_date"],
                weekly_meta["week_key"],
                written,
                skipped,
            )
            return {
                "ok": True,
                "reason": reason,
                "writable": True,
                "written": written,
                "skipped": skipped,
                "duration_ms": dur_ms,
                "daily_date": daily_meta["daily_date"],
                "week_key": weekly_meta["week_key"],
                "last_briefing_at": self._last_briefing_at,
                "stats": dict(self._last_briefing_stats),
            }

    def status(self) -> dict[str, Any]:
        with self._op_lock:
            if self._writable is None:
                ok, err = _probe_writable(self.export_dir)
                self._writable = ok
                if not ok and not self._last_error:
                    self._last_error = f"Vault 目录不可写: {err}"
            return {
                "enabled": bool(self._enabled),
                "briefing_enabled": bool(self._briefing_enabled),
                "writable": bool(self._writable),
                "export_dir": str(self.export_dir),
                "host_export_dir": self.host_export_dir,
                "snapshot_interval_seconds": self.snapshot_interval_seconds,
                "briefing_interval_seconds": self.briefing_interval_seconds,
                "last_export_at": self._last_export_at,
                "last_export_ts": self._last_export_ts,
                "last_export_reason": self._last_export_reason,
                "last_error": self._last_error,
                "stats": dict(self._last_stats),
                "briefing": {
                    "enabled": bool(self._briefing_enabled),
                    "interval_seconds": self.briefing_interval_seconds,
                    "last_briefing_at": self._last_briefing_at,
                    "last_briefing_ts": self._last_briefing_ts,
                    "last_briefing_reason": self._last_briefing_reason,
                    "daily_date": self._last_briefing_daily_date,
                    "week_key": self._last_briefing_week_key,
                    "stats": dict(self._last_briefing_stats),
                    "voice_push": False,
                },
            }


class ObsidianSyncRunner:
    """Asyncio coordinator for startup backfill, 5s debounced turn sync, and periodic snapshot polling."""

    def __init__(self, exporter: ObsidianExporter, *, debounce_seconds: float = 5.0):
        self.exporter = exporter
        self.debounce_seconds = max(0.1, float(debounce_seconds))
        self._dirty_dates: set[str] = set()
        self._dirty_snapshot: bool = False
        self._dirty_db: bool = False
        self._wake_event: asyncio.Event | None = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._startup_done: bool = False

    def start(self) -> None:
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="obsidian-sync-runner")

    def notify_turn(
        self, occurred_at: float | None = None, *, extra_occurred_at: float | None = None
    ) -> None:
        if not self.exporter.is_enabled():
            return
        d_str = self.exporter.resolve_turn_date(occurred_at)
        if d_str:
            self._dirty_dates.add(d_str)
        if extra_occurred_at is not None:
            d_extra = self.exporter.resolve_turn_date(extra_occurred_at)
            if d_extra:
                self._dirty_dates.add(d_extra)
        self._dirty_snapshot = True
        if self._wake_event is not None:
            self._wake_event.set()

    def notify_db_change(self) -> None:
        if not self.exporter.is_enabled():
            return
        self._dirty_snapshot = True
        self._dirty_db = True
        if self._wake_event is not None:
            self._wake_event.set()

    async def set_enabled_async(self, enabled: bool) -> dict[str, Any]:
        status = await asyncio.to_thread(self.exporter.set_enabled, enabled)
        if enabled:
            if not self._startup_done:
                res = await asyncio.to_thread(self.exporter.sync_startup_backfill)
                if isinstance(res, dict) and res.get("ok"):
                    self._startup_done = True
            else:
                await asyncio.to_thread(self.exporter.sync_if_db_changed, reason="toggle_enabled")
            if self.exporter.is_briefing_enabled():
                await asyncio.to_thread(self.exporter.export_briefings, reason="toggle_enabled")
            status = self.exporter.status()
        return status

    async def set_briefing_enabled_async(self, briefing_enabled: bool) -> dict[str, Any]:
        status = await asyncio.to_thread(self.exporter.set_briefing_enabled, briefing_enabled)
        if briefing_enabled and self.exporter.is_enabled():
            await asyncio.to_thread(self.exporter.export_briefings, reason="briefing_toggle_enabled")
            status = self.exporter.status()
        return status

    async def run_briefings_now(self, *, reason: str = "manual_briefing", target_date: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.exporter.export_briefings,
            reason=reason,
            target_date=target_date,
        )

    async def rebuild_now(self) -> dict[str, Any]:
        result = await asyncio.to_thread(self.exporter.rebuild_all, reason="manual_rebuild")
        if isinstance(result, dict) and result.get("ok"):
            self._startup_done = True
            self._dirty_dates.clear()
            self._dirty_snapshot = False
            self._dirty_db = False
        return result

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._wake_event is not None:
            self._wake_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final bounded flush if any turn commits or Studio DB writes are still dirty when shutting down
        if self.exporter.is_enabled() and (self._dirty_dates or self._dirty_snapshot or self._dirty_db):
            dirty = set(self._dirty_dates)
            db_dirty = self._dirty_db or (self._dirty_snapshot and not dirty)
            self._dirty_dates.clear()
            self._dirty_snapshot = False
            self._dirty_db = False
            try:
                if db_dirty:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            self.exporter.sync_if_db_changed,
                            reason="shutdown_flush",
                            extra_dirty_dates=dirty,
                        ),
                        timeout=5.0,
                    )
                else:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            self.exporter.sync_incremental,
                            dirty,
                            include_snapshots=True,
                            reason="shutdown_flush",
                        ),
                        timeout=5.0,
                    )
            except Exception as exc:
                log.warning("Obsidian shutdown flush skipped: %s", exc)

    async def _loop(self) -> None:
        assert self._wake_event is not None and self._stop_event is not None
        if self.exporter.is_enabled():
            try:
                res = await asyncio.to_thread(self.exporter.sync_startup_backfill)
                if isinstance(res, dict) and res.get("ok"):
                    self._startup_done = True
            except Exception as exc:
                log.warning("Obsidian startup backfill error: %s", exc)
            if self.exporter.is_briefing_enabled():
                try:
                    await asyncio.to_thread(self.exporter.export_briefings, reason="startup_briefing")
                except Exception as exc:
                    log.warning("Obsidian startup briefing error: %s", exc)

        interval = float(self.exporter.snapshot_interval_seconds)
        brief_interval = float(self.exporter.briefing_interval_seconds)
        next_poll = time.monotonic() + interval
        next_briefing = time.monotonic() + brief_interval

        while not self._stop_event.is_set():
            now_mono = time.monotonic()
            wait_sec = max(0.2, min(next_poll, next_briefing) - now_mono)
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=wait_sec)
            except asyncio.TimeoutError:
                pass

            if self._stop_event.is_set():
                break

            if self._wake_event.is_set():
                self._wake_event.clear()
                # Debounce bursts of turns/updates cleanly
                await asyncio.sleep(self.debounce_seconds)
                if self._stop_event.is_set():
                    break
                if not self.exporter.is_enabled():
                    self._dirty_dates.clear()
                    self._dirty_snapshot = False
                    self._dirty_db = False
                    continue
                dirty = set(self._dirty_dates)
                snap_dirty = self._dirty_snapshot
                db_dirty = self._dirty_db
                self._dirty_dates.clear()
                self._dirty_snapshot = False
                self._dirty_db = False
                res: dict[str, Any] | None = None
                if db_dirty:
                    res = await asyncio.to_thread(
                        self.exporter.sync_if_db_changed,
                        reason="turn_commit" if dirty else "db_change",
                        extra_dirty_dates=dirty,
                    )
                elif dirty:
                    res = await asyncio.to_thread(
                        self.exporter.sync_incremental,
                        dirty,
                        include_snapshots=True,
                        reason="turn_commit",
                    )
                elif snap_dirty:
                    res = await asyncio.to_thread(
                        self.exporter.sync_if_db_changed,
                        reason="db_change",
                    )
                if isinstance(res, dict) and not res.get("ok"):
                    self._dirty_dates |= dirty
                    self._dirty_snapshot = self._dirty_snapshot or snap_dirty
                    self._dirty_db = self._dirty_db or db_dirty
                next_poll = time.monotonic() + interval
            elif time.monotonic() >= next_poll:
                next_poll = time.monotonic() + interval
                if self.exporter.is_enabled():
                    if not self._startup_done:
                        res = await asyncio.to_thread(self.exporter.sync_startup_backfill)
                        if isinstance(res, dict) and res.get("ok"):
                            self._startup_done = True
                    else:
                        await asyncio.to_thread(
                            self.exporter.sync_if_db_changed,
                            reason="snapshot_poll",
                        )
            if time.monotonic() >= next_briefing:
                next_briefing = time.monotonic() + brief_interval
                if self.exporter.is_enabled() and self.exporter.is_briefing_enabled():
                    try:
                        await asyncio.to_thread(
                            self.exporter.export_briefings,
                            reason="scheduled_briefing",
                        )
                    except Exception as exc:
                        log.warning("Obsidian scheduled briefing error: %s", exc)
