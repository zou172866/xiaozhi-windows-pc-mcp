"""Progressive `SKILL.md` metadata indexer & on-demand body loader (Phase 4).

Zero import-time side effects: no filesystem scans at import time.

Design invariants:
- Metadata-only indexing: scans `skills/*/SKILL.md` (read-only mount) and optional user
  `state/skills/*/SKILL.md`, parsing ONLY YAML frontmatter `name` & short `description`
  (<= 120 chars each, max 24 skills). Full skill bodies are NEVER loaded at boot or injected
  into static system prompts.
- Prefix-cache safe: renders a tiny `<available_skills>` summary designed to be injected
  strictly into `turn_context_router`'s ephemeral `<turn_context>` block, keeping the static
  system prompt 100% byte-identical across turns.
- Safe on-demand loading: `load_skill_body(name)` verifies canonical path containment
  (rejecting `..`, symlinks escaping roots), caps file size at 32 KB, and runs secret redaction.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import recall_guard
import runtime_settings
import xz_common

BUILTIN_SKILLS_DIR = os.path.join(xz_common.ROOT_DIR, "skills")
USER_SKILLS_DIR = os.path.join(xz_common.STATE_DIR, "skills")

MAX_SKILLS = 24
MAX_DESC_CHARS = 120
MAX_SKILL_BODY_BYTES = 32 * 1024
_SAFE_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_SECRET_REDACT_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[已脱敏密钥]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[已脱敏密钥]"),
    (re.compile(r"(?i)\b(bearer|token|apikey|api_key|secret|password)\s*[:=]\s*[^\s,;\"']{6,}"), r"\1=[已脱敏]"),
)


def _redact_text(raw: str) -> str:
    txt = str(raw or "")
    for pat, repl in _SECRET_REDACT_PATTERNS:
        txt = pat.sub(repl, txt)
    try:
        txt, _ = recall_guard.sanitize_text_for_llm(txt, enabled=True)
    except Exception:
        pass
    return txt


def parse_skill_frontmatter(raw_text: str, *, fallback_name: str) -> Tuple[str, str]:
    """Parse minimal YAML frontmatter `name` & `description` without requiring PyYAML."""
    name = fallback_name
    desc = ""
    m = _FRONTMATTER_RE.match(raw_text or "")
    if m:
        lines = m.group(1).splitlines()
        i = 0
        while i < len(lines):
            raw_line = lines[i]
            s = raw_line.strip()
            if not s or s.startswith("#") or raw_line[0].isspace() or ":" not in raw_line:
                i += 1
                continue
            k, v = raw_line.split(":", 1)
            key = k.strip().lower()
            val_raw = v.strip()
            val_scalar = re.sub(r"\s+#.*$", "", val_raw)
            if val_scalar == "" or re.fullmatch(r"[>|][+-]?\d*|[>|]\d*[+-]?", val_scalar):
                collected: List[str] = []
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if not nxt.strip():
                        i += 1
                        continue
                    if nxt[0].isspace():
                        collected.append(nxt.strip())
                        i += 1
                        continue
                    break
                val = " ".join(collected).strip()
            else:
                if len(val_raw) >= 2 and val_raw[0] == val_raw[-1] and val_raw[0] in ('"', "'"):
                    val = val_raw[1:-1].strip()
                else:
                    val = val_raw
                i += 1
            if key == "name" and val:
                name = val
            elif key == "description" and val:
                desc = val
    if not desc:
        # Fallback: first non-empty non-heading line of body
        body = raw_text[m.end() :] if m else (raw_text or "")
        for ln in body.splitlines():
            st = ln.strip().lstrip("#").strip()
            if st:
                desc = st
                break
    clean_name = re.sub(r"[^a-zA-Z0-9._-]", "-", str(name or fallback_name).strip())[:64] or fallback_name
    clean_desc = re.sub(r"\s+", " ", str(desc or "无简介")).strip()
    if len(clean_desc) > MAX_DESC_CHARS:
        clean_desc = clean_desc[: MAX_DESC_CHARS - 1].rstrip() + "…"
    return clean_name, clean_desc


def _is_within_root(path: str, root: str) -> bool:
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
        return os.path.commonpath([real_root, real_path]) == real_root
    except Exception:
        return False


class SkillRegistry:
    """Mtime-cached progressive SKILL.md index."""

    def __init__(
        self,
        *,
        roots: Optional[List[str]] = None,
        settings_path: Optional[str] = None,
    ) -> None:
        self._roots = list(roots) if roots is not None else [BUILTIN_SKILLS_DIR, USER_SKILLS_DIR]
        self.settings_path = settings_path
        self._lock = threading.RLock()
        self._index_sig: Optional[Tuple[Tuple[str, int, int], ...]] = None
        self._skills: Dict[str, Dict[str, Any]] = {}
        self._load_count: int = 0

    def _compute_sig(self) -> Tuple[Tuple[str, int, int], ...]:
        entries: List[Tuple[str, int, int]] = []
        for root in self._roots:
            if not root or not os.path.isdir(root):
                continue
            try:
                for entry in sorted(os.scandir(root), key=lambda e: e.name):
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    skill_file = os.path.join(entry.path, "SKILL.md")
                    if os.path.islink(skill_file) or not os.path.isfile(skill_file):
                        continue
                    try:
                        st = os.stat(skill_file)
                        entries.append((skill_file, st.st_mtime_ns, st.st_size))
                    except OSError:
                        continue
            except OSError:
                continue
        return tuple(entries)

    def scan(self, *, force: bool = False) -> List[Dict[str, Any]]:
        sig = self._compute_sig()
        with self._lock:
            if not force and self._index_sig == sig:
                return [dict(v) for v in self._skills.values()]

            discovered: Dict[str, Dict[str, Any]] = {}
            for root_idx, root in enumerate(self._roots):
                if not root or not os.path.isdir(root):
                    continue
                source_kind = "builtin" if root_idx == 0 else "user"
                try:
                    dirs = sorted(os.scandir(root), key=lambda e: e.name)
                except OSError:
                    continue
                for entry in dirs:
                    if len(discovered) >= MAX_SKILLS:
                        break
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                    skill_file = os.path.join(entry.path, "SKILL.md")
                    if os.path.islink(skill_file) or not os.path.isfile(skill_file):
                        continue
                    if not _is_within_root(skill_file, root):
                        continue
                    try:
                        st = os.stat(skill_file)
                        if st.st_size > MAX_SKILL_BODY_BYTES:
                            continue
                        with open(skill_file, "r", encoding="utf-8", errors="replace") as f:
                            head = f.read(2048)
                    except OSError:
                        continue
                    name, desc = parse_skill_frontmatter(head, fallback_name=entry.name)
                    if not _SAFE_SKILL_NAME_RE.match(name):
                        continue
                    discovered[name] = {
                        "name": name,
                        "description": _redact_text(desc),
                        "source": source_kind,
                        "path": skill_file,
                        "root": root,
                        "size_bytes": int(st.st_size),
                    }
            self._skills = discovered
            self._index_sig = sig
            return [dict(v) for v in self._skills.values()]

    def is_enabled(self) -> bool:
        return bool(runtime_settings.get_skill_loader(self.settings_path).get("enabled", True))

    def build_turn_context_index(self) -> str:
        """Render a compact `<available_skills>` summary for ephemeral `<turn_context>` injection."""
        if not self.is_enabled():
            return ""
        skills = self.scan()
        if not skills:
            return ""
        lines = [
            "<available_skills>",
            "以下为按需加载的操作技能清单（仅含摘要，勿盲猜细节；仅在当前任务需要时调用 `load_skill` 工具读取正文）：",
        ]
        for s in skills[:MAX_SKILLS]:
            lines.append(f"- {s['name']}: {s['description']}")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def load_skill_body(self, skill_name: str) -> Tuple[bool, str]:
        """Load full `SKILL.md` body on demand with canonical path & size guards."""
        target_name = str(skill_name or "").strip()
        if not _SAFE_SKILL_NAME_RE.match(target_name):
            return False, f"非法技能名称: {target_name!r}"
        self.scan()
        with self._lock:
            meta = self._skills.get(target_name)
        if not meta:
            avail = ", ".join(sorted(self._skills.keys())) or "无"
            return False, f"未找到技能 `{target_name}`（当前可用：{avail}）"
        path = meta["path"]
        root = meta["root"]
        if os.path.islink(path) or not _is_within_root(path, root):
            return False, f"安全拦截：技能文件 `{target_name}` 路径越界或包含符号链接"
        try:
            st = os.stat(path)
            if st.st_size > MAX_SKILL_BODY_BYTES:
                return False, f"技能文件 `{target_name}` 体积超过 {MAX_SKILL_BODY_BYTES} 字节上限"
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read(MAX_SKILL_BODY_BYTES)
        except OSError as exc:
            return False, f"读取技能 `{target_name}` 失败: {exc}"
        with self._lock:
            self._load_count += 1
        return True, _redact_text(body)

    def get_status(self, *, force_scan: bool = False) -> Dict[str, Any]:
        skills = self.scan(force=force_scan)
        summary = self.build_turn_context_index()
        return {
            "ok": True,
            "enabled": self.is_enabled(),
            "count": len(skills),
            "max_skills": MAX_SKILLS,
            "estimated_index_chars": len(summary),
            "on_demand_loads": self._load_count,
            "skills": [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "source": s["source"],
                    "size_bytes": s["size_bytes"],
                }
                for s in skills
            ],
        }


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------

_reg_lock = threading.Lock()
_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    global _registry
    with _reg_lock:
        if _registry is None:
            _registry = SkillRegistry()
        return _registry


def reset_skill_registry(registry: Optional[SkillRegistry] = None) -> None:
    global _registry
    with _reg_lock:
        _registry = registry
