"""Read-only Obsidian Inbox importer for Anima Brain (Phase 3 ⑥).

Design invariants:
- Strictly disabled by default (`inbox_enabled=False`): zero directory scans, zero SQLite writes,
  and zero receipt generation while disabled.
- Scans ONLY `<OBSIDIAN_EXPORT_DIR>/收件箱/` (rejects symlinks and path traversal outside inbox).
- NEVER modifies user-authored Markdown notes (eliminates Obsidian Sync / iCloud / OneDrive conflicts);
  writes a single read-only receipt `<OBSIDIAN_EXPORT_DIR>/收件箱/_同步状态.md` via hash-deduped atomic replace.
- Deterministic, zero-LLM Markdown parsing:
  * `- [ ] <text>` -> Anima Brain open loop (`loop_status="open"`, `followup="ask"`, optional `@YYYY-MM-DD`).
  * `- [x] <text>` -> resolves matching open loop (`loop_status="resolved"`) & dismisses proactive follow-up.
  * `- <text>` / plain memo lines -> durable Anima Brain fact with optional `#偏好/#目标/#关系/#日程/#边界` tags.
- Mandatory credential & sensitive-entity sanitization via `obsidian_exporter.redact_credentials` and
  `recall_sanitizer.sanitize_recall_context` before persisting to `brain.sqlite3`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from companion_engine.engine import Conflict
from companion_engine.models import CharacterSpec, MemoryDetails, MemoryDraft

import proactive_state
import recall_sanitizer

log = logging.getLogger("anima-obsidian-inbox")

INBOX_DIRNAME = "收件箱"
STATUS_FILENAME = "_同步状态.md"
MAX_FILES_PER_SCAN = 32
MAX_FILE_BYTES = 128 * 1024
MAX_ITEMS_PER_SCAN = 200

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_OBSIDIAN_COMMENT_RE = re.compile(r"%%.*?%%", re.DOTALL)
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_CHECKBOX_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[([ xX])\][ \t]+(.+?)[ \t]*$")
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d{1,4}[.)])[ \t]+(.+?)[ \t]*$")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}(?:[ \t]|$)")
_THEMATIC_BREAK_RE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$")
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_DATE_RE = re.compile(r"(?:@|截止[:：]?|日期[:：]?|due[:：]?)?\s*(\d{4}-\d{2}-\d{2})(?!\d)")
_INLINE_TAG_RE = re.compile(r"(?<!\w)#([^\s#，。！？、；：,.!?;:()（）\[\]【】]{1,20})")
_REDACTION_ONLY_RE = re.compile(r"^[\s\[\]【】()（）.,，。:;：；\-_已脱敏REDACTED*]+$", re.IGNORECASE)

_TAG_TO_CATEGORY: dict[str, str] = {
    "偏好": "preference",
    "喜好": "preference",
    "习惯": "preference",
    "preference": "preference",
    "目标": "goal",
    "计划": "goal",
    "待办": "goal",
    "任务": "goal",
    "提醒": "goal",
    "goal": "goal",
    "todo": "goal",
    "关系": "relationship",
    "家人": "relationship",
    "朋友": "relationship",
    "人际": "relationship",
    "relationship": "relationship",
    "日期": "date",
    "日程": "date",
    "纪念日": "date",
    "生日": "date",
    "date": "date",
    "边界": "boundary",
    "忌口": "boundary",
    "底线": "boundary",
    "禁忌": "boundary",
    "boundary": "boundary",
    "身份": "identity",
    "资料": "identity",
    "identity": "identity",
    "风格": "style",
    "沟通": "style",
    "style": "style",
    "情绪": "emotional",
    "心情": "emotional",
    "emotional": "emotional",
    "备忘": "event",
    "事件": "event",
    "记录": "event",
    "event": "event",
}


def ensure_inbox_ledger_schema(db) -> None:
    """Create the lightweight idempotency ledger inside `brain.sqlite3` (only called when enabled)."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS xiaozhi_obsidian_inbox_items (
            owner TEXT NOT NULL,
            character_id TEXT NOT NULL,
            item_hash TEXT NOT NULL,
            memory_id TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'fact',
            status TEXT NOT NULL DEFAULT 'active',
            text_preview TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (owner, character_id, item_hash)
        )
        """
    )


def _extract_valid_date(text: str) -> tuple[date | None, str]:
    """Extract first valid `YYYY-MM-DD` date token (returning `(date_obj, cleaned_text)`)."""
    for m in _DATE_RE.finditer(text):
        raw_d = m.group(1)
        try:
            parsed = datetime.strptime(raw_d, "%Y-%m-%d").date()
        except ValueError:
            continue
        # Remove leading `@YYYY-MM-DD` / `due:YYYY-MM-DD` prefix token cleanly while keeping prose dates intact
        full_tok = m.group(0)
        if full_tok.lstrip().startswith(("@", "截止", "日期", "due", "DUE")):
            cleaned = (text[: m.start()] + " " + text[m.end() :]).strip()
            cleaned = re.sub(r"\s{2,}", " ", cleaned)
            return parsed, cleaned or text
        return parsed, text
    return None, text


def _extract_category_and_clean(text: str, *, kind: str, has_date: bool) -> tuple[str, str]:
    detected: str | None = None

    def _replace_tag(m: re.Match[str]) -> str:
        nonlocal detected
        tag = m.group(1).strip().lower()
        cat = _TAG_TO_CATEGORY.get(tag)
        if cat:
            if detected is None:
                detected = cat
            return ""
        return m.group(0)

    cleaned = _INLINE_TAG_RE.sub(_replace_tag, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if detected:
        return detected, cleaned
    if kind == "todo":
        return ("date" if has_date else "goal"), cleaned
    return ("date" if has_date else "event"), cleaned


def _sanitize_memo_text(raw: str, redact_fn) -> tuple[str, bool]:
    """Redact credentials & sensitive entities; returns `(sanitized_text, had_redaction)`."""
    step1 = redact_fn(str(raw or ""))
    step2, stats = recall_sanitizer.redact_sensitive_entities(step1)
    cleaned = " ".join(step2.split()).strip()
    had_redaction = (step1 != str(raw or "")) or bool(stats.get("redacted_count", 0))
    return cleaned, had_redaction


def _canonical_item_hash(kind: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(f"{kind}:{norm}".encode("utf-8")).hexdigest()[:24]


def parse_inbox_markdown(
    raw_text: str,
    *,
    source_name: str,
    redact_fn,
    max_items: int = MAX_ITEMS_PER_SCAN,
) -> tuple[list[dict[str, Any]], int]:
    """Deterministically extract memo facts and todo checkboxes from an Obsidian Inbox `.md` note."""
    if not raw_text or not raw_text.strip():
        return [], 0

    body = _FRONTMATTER_RE.sub("", raw_text)
    body = _HTML_COMMENT_RE.sub("", body)
    body = _OBSIDIAN_COMMENT_RE.sub("", body)

    items: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    redacted_skipped = 0
    in_fence = False
    fence_char = ""

    for raw_line in body.splitlines():
        if len(items) >= max_items:
            break
        fm = _FENCE_RE.match(raw_line)
        if fm:
            marker = fm.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_char = marker
            elif marker == fence_char:
                in_fence = False
                fence_char = ""
            continue
        if in_fence:
            continue

        line = raw_line.strip()
        if not line:
            continue
        if _HEADING_RE.match(line) or _THEMATIC_BREAK_RE.match(line) or _TABLE_ROW_RE.match(line):
            continue
        if line.startswith(">") or "::" in line[:32]:
            continue

        kind = "fact"
        checked = False
        content = ""
        m_chk = _CHECKBOX_RE.match(raw_line)
        if m_chk:
            kind = "todo"
            checked = m_chk.group(1).lower() == "x"
            content = m_chk.group(2).strip()
        else:
            m_bul = _BULLET_RE.match(raw_line)
            if m_bul:
                kind = "fact"
                content = m_bul.group(1).strip()
            elif len(line) >= 4 and not line.startswith("![["):
                kind = "fact"
                content = line
            else:
                continue

        if not content:
            continue

        ev_date, content_no_date = _extract_valid_date(content)
        category, content_clean = _extract_category_and_clean(
            content_no_date,
            kind=kind,
            has_date=ev_date is not None,
        )
        safe_text, had_redaction = _sanitize_memo_text(content_clean, redact_fn)
        if len(safe_text) < 2 or _REDACTION_ONLY_RE.match(safe_text):
            if had_redaction:
                redacted_skipped += 1
            continue

        safe_text = safe_text[:500]
        ihash = _canonical_item_hash(kind, safe_text)
        if ihash in seen_hashes:
            # If the same file lists the same todo twice with one checked, keep checked state
            if kind == "todo" and checked:
                for prev in items:
                    if prev["item_hash"] == ihash:
                        prev["checked"] = True
                        break
            continue
        seen_hashes.add(ihash)
        items.append(
            {
                "item_hash": ihash,
                "kind": kind,
                "checked": checked,
                "text": safe_text,
                "category": category,
                "event_date": ev_date,
                "source_file": source_name,
                "had_redaction": had_redaction,
            }
        )

    return items, redacted_skipped


def list_safe_inbox_files(inbox_dir: Path) -> tuple[list[Path], list[str]]:
    """Return sorted regular `.md` files strictly inside `inbox_dir` (no symlinks, no `_`/`.` files)."""
    warnings: list[str] = []
    if not inbox_dir.exists() or not inbox_dir.is_dir():
        return [], warnings
    try:
        root_resolved = inbox_dir.resolve()
    except OSError as exc:
        warnings.append(f"无法解析收件箱目录: {exc}")
        return [], warnings

    files: list[Path] = []
    try:
        candidates = sorted(inbox_dir.rglob("*.md"), key=lambda p: str(p).lower())
    except OSError as exc:
        warnings.append(f"扫描收件箱失败: {exc}")
        return [], warnings

    for p in candidates:
        if len(files) >= MAX_FILES_PER_SCAN:
            warnings.append(f"收件箱文件数超过单次上限 {MAX_FILES_PER_SCAN}，已截断其余文件")
            break
        try:
            # Skip hidden/system dirs or files (e.g. `_同步状态.md`, `.obsidian`)
            rel_parts = p.relative_to(inbox_dir).parts
            if any(part.startswith((".", "_")) for part in rel_parts):
                continue
            if p.is_symlink():
                warnings.append(f"已跳过符号链接: {p.name}")
                continue
            resolved = p.resolve()
            if not resolved.is_relative_to(root_resolved):
                warnings.append(f"已拦截越界路径: {p.name}")
                continue
            if not resolved.is_file():
                continue
            st = resolved.stat()
            if st.st_size > MAX_FILE_BYTES:
                warnings.append(f"文件 {p.name} 超过 {MAX_FILE_BYTES // 1024} KiB 上限，已跳过")
                continue
            files.append(resolved)
        except OSError:
            continue
    return files, warnings


def compute_inbox_fingerprint(inbox_dir: Path) -> str:
    """Fast O(files) mtime+size fingerprint for zero-SQLite-lock polling short-circuit."""
    files, _ = list_safe_inbox_files(inbox_dir)
    h = hashlib.sha256()
    for f in files:
        try:
            st = f.stat()
            rel = f.relative_to(inbox_dir.resolve()).as_posix()
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode("utf-8"))
        except OSError:
            continue
    return h.hexdigest()[:24]


def _render_status_receipt(
    *,
    character_name: str,
    tz: ZoneInfo,
    last_scan_ts: float,
    stats: dict[str, Any],
    recent_rows: list[dict[str, Any]],
    warnings: list[str],
) -> str:
    ts_str = datetime.fromtimestamp(last_scan_ts, tz=tz).strftime("%Y-%m-%d %H:%M:%S") if last_scan_ts else "--"
    lines = [
        "---",
        "type: 小智收件箱同步状态",
        f"updated: {ts_str}",
        f"active_facts: {int(stats.get('total_facts', 0))}",
        f"open_todos: {int(stats.get('total_open_todos', 0))}",
        f"resolved_todos: {int(stats.get('total_resolved_todos', 0))}",
        "tags: [小智/收件箱]",
        "---",
        "",
        f"# {character_name} · Obsidian 双向备忘收件箱状态",
        "",
        "> [!tip] 本文件由 Anima Brain 只读自动维护，**绝不会修改你写的任何原始备忘文件**（零网盘/Obsidian Sync 冲突）。",
        "",
        "## 使用方法",
        f"- 在当前 `{INBOX_DIRNAME}/` 目录新建任意 `.md` 笔记（如 `给芽衣的备忘.md`），按行写入：",
        "  - `- [ ] 提醒我周五把周报发给老板 @2026-12-04` → 同步为「未完待续」，后续对话可自然跟进",
        "  - 把上面改成 `- [x] ...` → 下次扫描自动消标完成，不再追问",
        "  - `- 最近在戒糖，晚饭少吃碳水 #偏好` → 同步为长期记忆（支持 `#偏好` `#目标` `#关系` `#日程` `#边界` `#情绪`）",
        "- 密钥、密码、手机号、验证码等高敏信息会在入库前自动脱敏拦截。",
        "",
        "## 最近一次扫描统计",
        f"- 扫描时间：{ts_str}",
        f"- 扫描文件：**{int(stats.get('scanned_files', 0))}** 个",
        f"- 本次新增：长期记忆 **{int(stats.get('added_facts', 0))}** 条 · 待办跟进 **{int(stats.get('added_loops', 0))}** 项 · 消标完成 **{int(stats.get('resolved_loops', 0))}** 项",
        f"- 累计已同步：长期记忆 **{int(stats.get('total_facts', 0))}** 条 · 未完待办 **{int(stats.get('total_open_todos', 0))}** 项 · 已完成 **{int(stats.get('total_resolved_todos', 0))}** 项",
    ]
    if warnings:
        lines.append("")
        lines.append("## 提示与跳过项")
        for w in warnings[:10]:
            lines.append(f"- {w}")

    lines.append("")
    lines.append("## 最近同步记录（最多 20 条）")
    if not recent_rows:
        lines.append("暂未扫描到备忘条目。在当前目录新建 `.md` 写入几条备忘即可。")
    else:
        for row in recent_rows[:20]:
            kind_label = "待办" if row["kind"] == "todo" else "记忆"
            st_badge = {"open": "待跟进", "resolved": "已完成", "active": "已入库"}.get(row["status"], row["status"])
            ev = f" · 日期 {row['event_date']}" if row.get("event_date") else ""
            lines.append(f"- [{kind_label}·{st_badge}] {row['text_preview']}（来源：`{row['source_file']}`{ev}）")
    lines.append("")
    return "\n".join(lines)


def scan_obsidian_inbox(
    brain: Any,
    *,
    character_id: str,
    export_dir: Path,
    redact_fn,
    write_atomic_fn,
    force: bool = False,
    last_fingerprint: str = "",
) -> dict[str, Any]:
    """Scan `<export_dir>/收件箱/*.md`, upsert facts/todos into Anima Brain, and write read-only receipt."""
    started = time.monotonic()
    inbox_dir = export_dir / INBOX_DIRNAME
    status_path = inbox_dir / STATUS_FILENAME

    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "changed": False,
            "error": f"无法创建或访问收件箱目录: {redact_fn(str(exc))[:160]}",
            "fingerprint": last_fingerprint,
        }

    fp = compute_inbox_fingerprint(inbox_dir)
    if not force and fp == last_fingerprint and status_path.is_file():
        return {
            "ok": True,
            "changed": False,
            "skipped_unchanged": True,
            "fingerprint": fp,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        }

    files, warnings = list_safe_inbox_files(inbox_dir)
    inbox_root = inbox_dir.resolve()
    parsed_items: list[dict[str, Any]] = []
    seen_global: set[str] = set()
    redacted_skipped = 0
    remaining = MAX_ITEMS_PER_SCAN

    for fpath in files:
        if remaining <= 0:
            break
        try:
            raw_text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"读取 {fpath.name} 失败: {exc}")
            continue
        rel_name = fpath.relative_to(inbox_root).as_posix()
        file_items, red_skip = parse_inbox_markdown(
            raw_text,
            source_name=rel_name,
            redact_fn=redact_fn,
            max_items=remaining,
        )
        redacted_skipped += red_skip
        for it in file_items:
            ih = it["item_hash"]
            if ih in seen_global:
                if it["kind"] == "todo" and it["checked"]:
                    for prev in parsed_items:
                        if prev["item_hash"] == ih:
                            prev["checked"] = True
                            break
                continue
            seen_global.add(ih)
            parsed_items.append(it)
            remaining -= 1

    if redacted_skipped > 0:
        warnings.append(f"已自动跳过 {redacted_skipped} 条纯敏感凭据内容")

    engine = brain.engine
    now = time.time()
    added_facts = 0
    added_loops = 0
    resolved_loops = 0
    skipped_existing = 0

    pending_proactive_acks: list[tuple[str, str]] = []
    with engine._lock:
        char_row = engine._character(character_id)
        spec = CharacterSpec.model_validate_json(char_row["spec"])
        tz_name = str(getattr(spec, "timezone", "") or "Asia/Shanghai")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")

        ensure_inbox_ledger_schema(engine.db)
        engine.db.commit()

        existing_rows = {
            r[0]: {"memory_id": r[1], "kind": r[2], "status": r[3]}
            for r in engine.db.execute(
                "SELECT item_hash, memory_id, kind, status FROM xiaozhi_obsidian_inbox_items "
                "WHERE owner=? AND character_id=?",
                (engine.owner, character_id),
            ).fetchall()
        }

        for item in parsed_items:
            ih = item["item_hash"]
            kind = item["kind"]
            checked = bool(item["checked"])
            text = item["text"]
            cat = item["category"]
            ev_date = item["event_date"]
            ev_date_str = ev_date.isoformat() if isinstance(ev_date, date) else ""
            src_file = item["source_file"]
            mem_key = f"obs_inbox:{ih[:12]}"
            prev = existing_rows.get(ih)

            if kind == "fact":
                if prev is not None:
                    skipped_existing += 1
                    continue
                draft = MemoryDraft(
                    key=mem_key,
                    text=text,
                    category=cat,
                    source_quote=f"Obsidian收件箱({src_file}): {text[:180]}",
                    details=MemoryDetails(
                        subject="user",
                        temporal_kind="event" if ev_date else "current",
                        event_date=ev_date,
                        importance=6,
                        loop_status="none",
                        followup="background_only",
                    ),
                )
                try:
                    mem = engine.remember(character_id, draft)
                    mem_id = mem.id
                except Conflict:
                    row = engine.db.execute(
                        "SELECT id FROM memories WHERE owner=? AND character_id=? AND key=?",
                        (engine.owner, character_id, mem_key),
                    ).fetchone()
                    mem_id = str(row[0]) if row else ""
                engine.db.execute(
                    "INSERT OR REPLACE INTO xiaozhi_obsidian_inbox_items "
                    "(owner, character_id, item_hash, memory_id, source_file, kind, status, text_preview, event_date, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (engine.owner, character_id, ih, mem_id, src_file, "fact", "active", text[:120], ev_date_str, now),
                )
                engine.db.commit()
                existing_rows[ih] = {"memory_id": mem_id, "kind": "fact", "status": "active"}
                added_facts += 1
                continue

            # kind == "todo"
            if not checked:
                if prev is None:
                    draft = MemoryDraft(
                        key=mem_key,
                        text=text,
                        category=cat,
                        source_quote=f"Obsidian收件箱({src_file}): {text[:180]}",
                        details=MemoryDetails(
                            subject="user",
                            temporal_kind="event" if ev_date else "current",
                            event_date=ev_date,
                            importance=7,
                            loop_status="open",
                            followup="ask",
                        ),
                    )
                    try:
                        mem = engine.remember(character_id, draft)
                        mem_id = mem.id
                    except Conflict:
                        row = engine.db.execute(
                            "SELECT id FROM memories WHERE owner=? AND character_id=? AND key=?",
                            (engine.owner, character_id, mem_key),
                        ).fetchone()
                        mem_id = str(row[0]) if row else ""
                    engine.db.execute(
                        "INSERT OR REPLACE INTO xiaozhi_obsidian_inbox_items "
                        "(owner, character_id, item_hash, memory_id, source_file, kind, status, text_preview, event_date, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (engine.owner, character_id, ih, mem_id, src_file, "todo", "open", text[:120], ev_date_str, now),
                    )
                    engine.db.commit()
                    existing_rows[ih] = {"memory_id": mem_id, "kind": "todo", "status": "open"}
                    added_loops += 1
                elif prev["status"] == "resolved" and prev["memory_id"]:
                    # User unchecked a previously resolved todo -> reopen cleanly
                    row = engine.db.execute(
                        "SELECT * FROM memories WHERE owner=? AND character_id=? AND id=?",
                        (engine.owner, character_id, prev["memory_id"]),
                    ).fetchone()
                    if row is not None:
                        cur_mem = engine._memory(row)
                        reopen_draft = MemoryDraft(
                            key=cur_mem.key,
                            text=text,
                            category=cur_mem.category,
                            source_quote=f"Obsidian收件箱重开({src_file}): {text[:180]}",
                            details=cur_mem.details.model_copy(
                                update={"loop_status": "open", "followup": "ask", "event_date": ev_date}
                            ),
                        )
                        engine.correct_memory(
                            character_id,
                            cur_mem.id,
                            reopen_draft,
                            expected_revision=cur_mem.revision,
                        )
                        pending_proactive_acks.append((cur_mem.id, "reset"))
                    engine.db.execute(
                        "UPDATE xiaozhi_obsidian_inbox_items SET status='open', source_file=?, updated_at=? "
                        "WHERE owner=? AND character_id=? AND item_hash=?",
                        (src_file, now, engine.owner, character_id, ih),
                    )
                    engine.db.commit()
                    existing_rows[ih]["status"] = "open"
                    added_loops += 1
                else:
                    skipped_existing += 1
            else:
                # Checked todo `- [x]`
                if prev is not None and prev["status"] == "open":
                    mem_id = prev["memory_id"]
                    if mem_id:
                        row = engine.db.execute(
                            "SELECT * FROM memories WHERE owner=? AND character_id=? AND id=?",
                            (engine.owner, character_id, mem_id),
                        ).fetchone()
                        if row is not None:
                            cur_mem = engine._memory(row)
                            done_draft = MemoryDraft(
                                key=cur_mem.key,
                                text=cur_mem.text,
                                category=cur_mem.category,
                                source_quote=f"Obsidian收件箱勾选完成({src_file}): {text[:180]}",
                                details=cur_mem.details.model_copy(
                                    update={"loop_status": "resolved", "followup": "background_only"}
                                ),
                            )
                            engine.correct_memory(
                                character_id,
                                cur_mem.id,
                                done_draft,
                                expected_revision=cur_mem.revision,
                            )
                            pending_proactive_acks.append((cur_mem.id, "dismiss"))
                    engine.db.execute(
                        "UPDATE xiaozhi_obsidian_inbox_items SET status='resolved', source_file=?, updated_at=? "
                        "WHERE owner=? AND character_id=? AND item_hash=?",
                        (src_file, now, engine.owner, character_id, ih),
                    )
                    engine.db.commit()
                    existing_rows[ih]["status"] = "resolved"
                    resolved_loops += 1
                elif prev is None:
                    # Already-checked historical item -> record as resolved without spawning a stale open loop
                    engine.db.execute(
                        "INSERT OR REPLACE INTO xiaozhi_obsidian_inbox_items "
                        "(owner, character_id, item_hash, memory_id, source_file, kind, status, text_preview, event_date, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (engine.owner, character_id, ih, "", src_file, "todo", "resolved", text[:120], ev_date_str, now),
                    )
                    engine.db.commit()
                    existing_rows[ih] = {"memory_id": "", "kind": "todo", "status": "resolved"}
                    skipped_existing += 1
                else:
                    skipped_existing += 1

        # Aggregate totals & recent items for receipt & 18080 status
        counts = dict(
            engine.db.execute(
                "SELECT kind || ':' || status, COUNT(*) FROM xiaozhi_obsidian_inbox_items "
                "WHERE owner=? AND character_id=? GROUP BY kind, status",
                (engine.owner, character_id),
            ).fetchall()
        )
        recent_rows = [
            {
                "kind": r[0],
                "status": r[1],
                "text_preview": r[2],
                "source_file": r[3],
                "event_date": r[4],
                "updated_at": float(r[5] or 0.0),
            }
            for r in engine.db.execute(
                "SELECT kind, status, text_preview, source_file, event_date, updated_at "
                "FROM xiaozhi_obsidian_inbox_items WHERE owner=? AND character_id=? "
                "ORDER BY updated_at DESC LIMIT 20",
                (engine.owner, character_id),
            ).fetchall()
        ]
        char_name = spec.name

    for ack_mid, ack_act in pending_proactive_acks:
        try:
            proactive_state.ack_proactive_event(
                brain,
                character_id,
                open_loop_id=ack_mid,
                action=ack_act,
                reason="obsidian_inbox",
                now=now,
            )
        except Exception as exc:
            log.warning("Obsidian inbox proactive ack (%s) skipped: %s", ack_act, exc)

    stats = {
        "scanned_files": len(files),
        "parsed_items": len(parsed_items),
        "added_facts": added_facts,
        "added_loops": added_loops,
        "resolved_loops": resolved_loops,
        "skipped_existing": skipped_existing,
        "redacted_skipped": redacted_skipped,
        "total_facts": int(counts.get("fact:active", 0)),
        "total_open_todos": int(counts.get("todo:open", 0)),
        "total_resolved_todos": int(counts.get("todo:resolved", 0)),
    }

    receipt_md = _render_status_receipt(
        character_name=char_name,
        tz=tz,
        last_scan_ts=now,
        stats=stats,
        recent_rows=recent_rows,
        warnings=warnings,
    )
    try:
        write_atomic_fn(status_path, receipt_md)
    except OSError as exc:
        warnings.append(f"写入状态回执失败: {redact_fn(str(exc))[:120]}")

    changed = (added_facts + added_loops + resolved_loops) > 0
    return {
        "ok": True,
        "changed": changed,
        "skipped_unchanged": False,
        "fingerprint": fp,
        "inbox_dir": str(inbox_dir),
        "receipt_path": str(status_path),
        "scanned_at": datetime.fromtimestamp(now, tz=tz).isoformat(timespec="seconds"),
        "scanned_ts": now,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "stats": stats,
        "warnings": warnings[:10],
        "recent_items": recent_rows[:12],
    }
