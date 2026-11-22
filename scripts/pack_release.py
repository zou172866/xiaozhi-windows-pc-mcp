"""Clean, audited out-of-the-box distribution packager for xiaozhi-server.

Builds a sanitized `.zip` archive suitable for sharing with other Windows users:
  1. Never includes `.env`, `secrets/`, `backups/`, `anima-data/`, `state/`,
     `config/runtime/`, `pc-mcp/state/`, `pc-mcp/.venv/`, `models/`, `voices/`,
     `logs/`, `*.bin`, `*.sqlite*`, or Dockerfile-generated static UI files.
  2. Fail-closed audit scans packaged files for accidental plaintext API keys,
     hardcoded personal paths, and `.ps1` (UTF-8 BOM + CRLF) / `.bat` (ASCII + CRLF)
     encoding violations.

Usage:
  python scripts/pack_release.py --dry-run
  python scripts/pack_release.py --output dist/xiaozhi-server-release.zip
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from typing import Iterable

SERVER_DIR = Path(__file__).resolve().parent.parent

FORBIDDEN_DIR_PREFIXES: tuple[str, ...] = (
    ".git/",
    ".venv/",
    "venv/",
    "env/",
    "dist/",
    "secrets/",
    "backups/",
    "anima-data/",
    "state/",
    "config/runtime/",
    "voices/",
    "logs/",
    "models/",
    "tmp/",
    "pc-mcp/state/",
    "pc-mcp/.venv/",
    "data/obsidian_export/",
    "data/voices/",
    "data/agy_tasks/",
    "data/anima/",
    "data/bin/",
)

FORBIDDEN_DIR_SEGMENTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
    }
)

FORBIDDEN_EXACT_PATHS = frozenset(
    {
        ".env",
        ".env.local",
        "docker-compose.override.yml",
        "data/.config.yaml",
        "data/chat_history.json",
        "data/.console_log_cursor.json",
        "data/l3_backend_config.json",
        "data/pc_control_mode.json",
        "data/tts_infer_xiaozhi.yaml",
        "data/.wakeup_words.yaml",
        "data/audio_output_mode.json",
        "data/reply_length_config.json",
        "data/runtime_settings.json",
        "data/pc_mcp_token.txt",
        "data/last_som_grounding.jpg",
        "anima-brain/src/companion_engine/static/memory-panel.html",
        "anima-brain/src/companion_engine/static/provider-settings.html",
        "anima-brain/src/companion_engine/static/voice-player.html",
        "anima-brain/src/companion_engine/static/voice-player-notices.txt",
    }
)

FORBIDDEN_GLOBS: tuple[str, ...] = (
    "*.bin",
    "*.sqlite",
    "*.sqlite3",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.bak",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ckpt",
    "*.pth",
    "*.onnx",
    "*.pt",
    "*.wav",
    "*.mp3",
    "*.log",
    "*.log.*",
    "*.pyc",
    "*.pyo",
    "data/nvs*",
    "data/*.jpg",
    "data/*.png",
    "data/*.migrated.bak",
)

_ALLOWED_DOTENV_FILES = frozenset({".env.example"})

_SECRET_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI/DeepSeek-style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("Google/Gemini API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")),
    ("Private key PEM block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)

_PERSONAL_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Hardcoded Windows user profile path",
        re.compile(r"(?i)\bC:\\Users\\(?!<|your|user|alice|bob|example|username\b)[A-Za-z0-9_.-]+\\"),
    ),
    ("Hardcoded personal Chinese project path", re.compile(r"(?i)\b[A-Z]:[\\/]故事[\\/]")),
    ("Hardcoded personal GSV weight filename", re.compile(r"芽衣[\\/._-]e\d+", re.IGNORECASE)),
)

_BINARY_SKIP_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".webp",
        ".svg",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".zip",
        ".gz",
        ".tar",
        ".pdf",
    }
)


def normalize_rel_path(rel_path: str) -> str:
    s = str(rel_path or "").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def is_forbidden_release_path(rel_path: str) -> str | None:
    """Return a non-empty reason if `rel_path` must NEVER enter a public distribution archive."""
    norm = normalize_rel_path(rel_path)
    if not norm:
        return "empty path"
    if norm in FORBIDDEN_EXACT_PATHS:
        return f"forbidden private/runtime file: {norm}"
    low = norm.lower()
    for prefix in FORBIDDEN_DIR_PREFIXES:
        if low.startswith(prefix.lower()):
            return f"forbidden private/runtime directory ({prefix.rstrip('/')})"
    parts = norm.split("/")
    for seg in parts[:-1]:
        if seg in FORBIDDEN_DIR_SEGMENTS:
            return f"forbidden directory segment ({seg})"
    name = parts[-1]
    if (name == ".env" or name.startswith(".env.")) and name not in _ALLOWED_DOTENV_FILES:
        return f"forbidden private env file ({name})"
    for pat in FORBIDDEN_GLOBS:
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(name, pat):
            return f"forbidden artifact pattern ({pat})"
    return None


def _is_test_file(norm_rel: str) -> bool:
    low = norm_rel.lower()
    name = low.rsplit("/", 1)[-1]
    return (
        low.startswith("tests/")
        or "/tests/" in low
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _git_ls_files(root: Path, *, tracked_only: bool = False) -> list[str]:
    cmd = ["git", "-C", str(root), "ls-files", "-z", "--cached"]
    if not tracked_only:
        cmd.extend(["--others", "--exclude-standard"])
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed (rc={proc.returncode}): {proc.stderr.decode('utf-8', 'replace')}")
    raw_items = [p.decode("utf-8", errors="surrogateescape") for p in proc.stdout.split(b"\x00") if p]
    return sorted({normalize_rel_path(p) for p in raw_items})


def collect_release_files(
    root: Path = SERVER_DIR,
    *,
    tracked_only: bool = False,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Collect candidate release files from git and separate any forbidden paths."""
    candidates = _git_ls_files(root, tracked_only=tracked_only)
    tracked_set = set(_git_ls_files(root, tracked_only=True))
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    tracked_violations: list[str] = []

    for rel in candidates:
        full = root / rel
        if not full.is_file():
            continue
        reason = is_forbidden_release_path(rel)
        if reason is not None:
            excluded.append((rel, reason))
            if rel in tracked_set:
                tracked_violations.append(f"{rel}: {reason}")
            continue
        included.append(rel)

    if tracked_violations:
        raise RuntimeError(
            "Fail-closed: git-tracked forbidden private/build files detected:\n  - "
            + "\n  - ".join(tracked_violations)
        )
    return included, excluded


def audit_release_files(root: Path, rel_paths: Iterable[str]) -> list[str]:
    """Audit all candidate files for encoding hygiene, accidental secrets, and forbidden paths."""
    issues: list[str] = []
    for raw_rel in rel_paths:
        rel = normalize_rel_path(raw_rel)
        forbid = is_forbidden_release_path(rel)
        if forbid is not None:
            issues.append(f"[FORBIDDEN] {rel}: {forbid}")
            continue

        fp = root / rel
        if not fp.is_file():
            issues.append(f"[MISSING] {rel}")
            continue

        raw = fp.read_bytes()
        low = rel.lower()

        # 1. Script encoding invariants (AGENTS.md redline)
        if low.endswith(".ps1"):
            if not raw.startswith(b"\xef\xbb\xbf"):
                issues.append(f"[ENCODING] {rel}: .ps1 must start with UTF-8 BOM")
            if b"\n" in raw.replace(b"\r\n", b""):
                issues.append(f"[ENCODING] {rel}: .ps1 must use CRLF line endings")
        elif low.endswith(".bat"):
            if any(b >= 128 for b in raw):
                issues.append(f"[ENCODING] {rel}: .bat must be pure ASCII")
            if b"\n" in raw.replace(b"\r\n", b""):
                issues.append(f"[ENCODING] {rel}: .bat must use CRLF line endings")

        # 2. Secret scan on non-test text files
        if fp.suffix.lower() in _BINARY_SKIP_SUFFIXES or b"\x00" in raw[:4096]:
            continue
        if _is_test_file(rel):
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue

        for label, pat in _SECRET_SCAN_PATTERNS:
            m = pat.search(text)
            if m:
                issues.append(f"[SECRET] {rel}: matched {label} ({m.group(0)[:10]}...)")

        for label, pat in _PERSONAL_LEAK_PATTERNS:
            m = pat.search(text)
            if m:
                issues.append(f"[PERSONAL] {rel}: matched {label} ({m.group(0)[:24]}...)")

    return issues


def build_release_zip(
    root: Path,
    rel_paths: Iterable[str],
    output_zip: Path,
    *,
    archive_prefix: str = "xiaozhi-server",
) -> tuple[int, int]:
    """Write audited files into a deterministic clean `.zip` archive."""
    prefix = archive_prefix.strip("/\\")
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = output_zip.with_suffix(output_zip.suffix + ".tmp")
    file_count = 0
    total_bytes = 0
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for raw_rel in sorted(rel_paths):
                rel = normalize_rel_path(raw_rel)
                fp = root / rel
                arcname = f"{prefix}/{rel}" if prefix else rel
                zf.write(fp, arcname=arcname)
                file_count += 1
                total_bytes += fp.stat().st_size
        os.replace(tmp_zip, output_zip)
    except Exception:
        if tmp_zip.exists():
            tmp_zip.unlink()
        raise
    return file_count, total_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package a clean, privacy-audited xiaozhi-server release archive")
    parser.add_argument(
        "--output",
        default="",
        help="Destination .zip path (default: dist/xiaozhi-server-release-YYYYMMDD.zip)",
    )
    parser.add_argument(
        "--prefix",
        default="xiaozhi-server",
        help="Top-level directory name inside the zip archive (default: xiaozhi-server)",
    )
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Include strictly git-tracked files (excludes untracked files)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run privacy/encoding audit and print summary without writing a zip file",
    )
    args = parser.parse_args(argv)

    try:
        files, excluded = collect_release_files(SERVER_DIR, tracked_only=args.tracked_only)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    issues = audit_release_files(SERVER_DIR, files)
    if issues:
        print("[FAIL] Release audit blocked packaging due to policy violations:", file=sys.stderr)
        for item in issues:
            print(f"  - {item}", file=sys.stderr)
        return 2

    total_bytes = sum((SERVER_DIR / f).stat().st_size for f in files)
    print(f"[AUDIT PASS] {len(files)} clean files ({total_bytes / (1024 * 1024):.2f} MiB), {len(excluded)} excluded")
    if args.dry_run:
        return 0

    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = SERVER_DIR / out_path
    else:
        stamp = datetime.date.today().strftime("%Y%m%d")
        out_path = SERVER_DIR / "dist" / f"xiaozhi-server-release-{stamp}.zip"

    count, nbytes = build_release_zip(SERVER_DIR, files, out_path, archive_prefix=args.prefix)
    zip_size = out_path.stat().st_size
    print(
        f"[OK] Packaged {count} files ({nbytes / (1024 * 1024):.2f} MiB raw -> "
        f"{zip_size / (1024 * 1024):.2f} MiB zip): {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
