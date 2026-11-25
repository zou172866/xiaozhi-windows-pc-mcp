"""Safe downloader for Sherpa-ONNX 3D-Speaker CAM++ Chinese speaker embedding model (Phase 2 ④).

Supports both direct CLI invocation on the Windows host (`python scripts/download_speaker_model.py`)
and in-container invocation from the 18080 Console one-click button.
Enforces strict HTTPS URL allowlist, minimum size validation, optional SHA-256 verification, and
atomic temp-file replace.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
import sys
import urllib.parse
import urllib.request
from typing import Callable, Optional, Tuple

MODEL_FILENAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
MIN_EXPECTED_BYTES = 20_000_000  # ~28 MB real model; reject truncated HTML/error pages
MAX_EXPECTED_BYTES = 120_000_000

DEFAULT_URLS: tuple[str, ...] = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "https://hf-mirror.com/csukuangfj/speaker-embedding-models/resolve/main/"
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
)

ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "hf-mirror.com",
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        "cdn-lfs-us-1.huggingface.co",
        "cdn-lfs.hf.co",
        "cas-bridge.xethub.hf.co",
        "modelscope.cn",
        "www.modelscope.cn",
    }
)


def is_allowed_download_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").strip().lower()
        if not host or host not in ALLOWED_HOSTS:
            return False
        port = parsed.port or 443
        return port == 443
    except ValueError:
        return False


def _validate_url_and_dns(url: str) -> str:
    clean = str(url or "").strip()
    if not is_allowed_download_url(clean):
        raise ValueError(f"URL host not in allowlist: {clean}")
    parsed = urllib.parse.urlparse(clean)
    host = (parsed.hostname or "").strip().lower()
    has_proxy = any(
        os.environ.get(k, "").strip()
        for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    )
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        for _fam, _type, _proto, _canon, sockaddr in infos:
            ip_obj = ipaddress.ip_address(sockaddr[0])
            if not ip_obj.is_global:
                raise ValueError(f"Refusing non-global IP for host: {host}")
    except socket.gaierror:
        if not has_proxy:
            raise
    return clean


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect hop BEFORE connecting (anti-SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url_and_dns(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_speaker_model(
    target_dir: str | Path,
    *,
    urls: Optional[Tuple[str, ...]] = None,
    expected_sha256: Optional[str] = None,
    timeout: float = 120.0,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Tuple[bool, str]:
    """Download the Sherpa-ONNX CAM++ speaker model atomically into `target_dir`."""
    try:
        dest_dir = Path(target_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / MODEL_FILENAME
        if dest_file.is_file() and dest_file.stat().st_size >= MIN_EXPECTED_BYTES:
            return True, str(dest_file)
    except OSError as exc:
        return False, f"Filesystem error: {exc}"

    cand_urls = tuple(urls) if urls else DEFAULT_URLS
    env_override = os.environ.get("XIAOZHI_SPEAKER_MODEL_URL", "").strip()
    if env_override:
        cand_urls = (env_override,) + cand_urls
    eff_sha = (expected_sha256 or os.environ.get("XIAOZHI_SPEAKER_MODEL_SHA256", "")).strip().lower()

    opener = urllib.request.build_opener(_SafeRedirectHandler)
    last_err = "no download URLs configured"
    for url in cand_urls:
        tmp_path = dest_dir / f".{MODEL_FILENAME}.{os.getpid()}.tmp"
        try:
            clean_url = _validate_url_and_dns(url)
            req = urllib.request.Request(
                clean_url,
                headers={"User-Agent": "xiaozhi-server-voiceprint-downloader/1.0"},
            )
            hasher = hashlib.sha256()
            written = 0
            with opener.open(req, timeout=timeout) as resp:
                final_url = resp.geturl()
                _validate_url_and_dns(final_url)
                total = int(resp.headers.get("Content-Length") or 0)
                with open(tmp_path, "wb") as out:
                    while True:
                        chunk = resp.read(128 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_EXPECTED_BYTES:
                            raise ValueError("Downloaded file exceeds safety size cap")
                        out.write(chunk)
                        hasher.update(chunk)
                        if callable(progress_cb):
                            progress_cb(written, total)
                    out.flush()
                    os.fsync(out.fileno())
            if written < MIN_EXPECTED_BYTES:
                raise ValueError(f"Downloaded file too small ({written} bytes)")
            digest = hasher.hexdigest().lower()
            if eff_sha and digest != eff_sha:
                raise ValueError(f"SHA-256 mismatch: expected {eff_sha}, got {digest}")
            os.replace(tmp_path, dest_file)
            return True, str(dest_file)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    return False, last_err


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download Sherpa-ONNX CAM++ speaker model (~28 MB)")
    default_dir = Path(__file__).resolve().parent.parent / "models" / "sherpa-onnx-speaker"
    parser.add_argument("--dir", default=str(default_dir), help="Target directory for speaker ONNX model")
    parser.add_argument("--sha256", default="", help="Optional expected SHA-256 digest")
    args = parser.parse_args(argv)

    def _prog(done: int, total: int) -> None:
        if total > 0:
            pct = min(100.0, done * 100.0 / total)
            print(f"\rDownloading {MODEL_FILENAME}: {pct:5.1f}% ({done // 1024} / {total // 1024} KiB)", end="", flush=True)

    ok, detail = download_speaker_model(args.dir, expected_sha256=args.sha256 or None, progress_cb=_prog)
    print()
    if ok:
        print(f"[OK] Speaker model ready: {detail}")
        return 0
    print(f"[FAIL] Download failed: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
