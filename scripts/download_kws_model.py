"""Safe downloader for Sherpa-ONNX WenetSpeech 3.3M int8 Chinese streaming KWS model.

Supports direct CLI invocation on the host (`python scripts/download_kws_model.py`) and
reuse from container tooling. Enforces strict HTTPS host allowlist, anti-SSRF global IP +
redirect validation, safe tar.bz2 extraction (no path traversal / symlinks), size bounds,
and atomic file replacement into `models/sherpa-onnx-kws/`.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
import tarfile
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional, Tuple

ARCHIVE_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01-mobile.tar.bz2"
MIN_EXPECTED_BYTES = 5_000_000  # ~15.3 MB real tar.bz2; reject HTML/error responses
MAX_EXPECTED_BYTES = 80_000_000

DEFAULT_URLS: tuple[str, ...] = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01-mobile.tar.bz2",
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


_TUN_FAKE_IP4_NET = ipaddress.ip_network("198.18.0.0/15")
_TUN_FAKE_IP6_NET = ipaddress.ip_network("2001:2::/48")


def _is_permitted_resolved_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip_obj.is_global:
        return True
    # Allow RFC 2544 / RFC 5180 benchmark ranges used by local Clash/Mihomo TUN fake-IP DNS for allowlisted HTTPS hosts
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _TUN_FAKE_IP4_NET:
        return True
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in _TUN_FAKE_IP6_NET:
        return True
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
            if not _is_permitted_resolved_ip(ip_obj):
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


def resolve_kws_model_files(model_dir: str | Path) -> Optional[Dict[str, str]]:
    """Return `{tokens, encoder, decoder, joiner}` paths if all 4 required files exist in `model_dir`."""
    base = Path(model_dir)
    if not base.is_dir():
        return None
    tokens = base / "tokens.txt"
    if not tokens.is_file() or tokens.stat().st_size <= 0:
        return None

    def _pick(prefix: str) -> Optional[Path]:
        cands = sorted(
            p for p in base.iterdir()
            if p.is_file() and p.name.startswith(prefix) and p.name.endswith(".onnx") and p.stat().st_size > 0
        )
        if not cands:
            return None
        int8_cands = [p for p in cands if p.name.endswith(".int8.onnx")]
        return int8_cands[0] if int8_cands else cands[0]

    enc = _pick("encoder-")
    dec = _pick("decoder-")
    joi = _pick("joiner-")
    if not (enc and dec and joi):
        return None
    return {
        "tokens": str(tokens),
        "encoder": str(enc),
        "decoder": str(dec),
        "joiner": str(joi),
    }


def _select_archive_members(tf: tarfile.TarFile) -> Dict[str, tarfile.TarInfo]:
    """Select only the 4 needed regular files (`tokens.txt`, `encoder`, `decoder`, `joiner`) safely."""
    candidates: Dict[str, list[tarfile.TarInfo]] = {
        "tokens": [],
        "encoder": [],
        "decoder": [],
        "joiner": [],
    }
    for m in tf.getmembers():
        if not m.isfile() or m.issym() or m.islnk():
            continue
        raw_name = (m.name or "").replace("\\", "/")
        if raw_name.startswith("/") or ".." in raw_name.split("/"):
            raise ValueError(f"Unsafe archive member path: {m.name}")
        if m.size <= 0 or m.size > MAX_EXPECTED_BYTES:
            continue
        base = os.path.basename(raw_name)
        if base == "tokens.txt":
            candidates["tokens"].append(m)
        elif base.startswith("encoder-") and base.endswith(".onnx"):
            candidates["encoder"].append(m)
        elif base.startswith("decoder-") and base.endswith(".onnx"):
            candidates["decoder"].append(m)
        elif base.startswith("joiner-") and base.endswith(".onnx"):
            candidates["joiner"].append(m)

    picked: Dict[str, tarfile.TarInfo] = {}
    for role, items in candidates.items():
        if not items:
            raise ValueError(f"Archive missing required KWS file for role={role}")
        items.sort(key=lambda x: os.path.basename(x.name))
        if role in ("encoder", "decoder", "joiner"):
            int8 = [x for x in items if os.path.basename(x.name).endswith(".int8.onnx")]
            picked[role] = int8[0] if int8 else items[0]
        else:
            picked[role] = items[0]
    return picked


def _safe_extract_kws_archive(archive_path: Path, dest_dir: Path) -> Dict[str, str]:
    extracted_paths: Dict[str, str] = {}
    with tarfile.open(archive_path, mode="r:bz2") as tf:
        picked = _select_archive_members(tf)
        for role, member in picked.items():
            base = os.path.basename(member.name)
            tmp_out = dest_dir / f".{base}.{os.getpid()}.tmp"
            final_out = dest_dir / base
            src_fh = tf.extractfile(member)
            if src_fh is None:
                raise ValueError(f"Failed to read archive member: {member.name}")
            try:
                with src_fh, open(tmp_out, "wb") as out_fh:
                    written = 0
                    while True:
                        buf = src_fh.read(128 * 1024)
                        if not buf:
                            break
                        written += len(buf)
                        if written > MAX_EXPECTED_BYTES:
                            raise ValueError(f"Extracted member exceeds size cap: {base}")
                        out_fh.write(buf)
                    out_fh.flush()
                    os.fsync(out_fh.fileno())
                os.replace(tmp_out, final_out)
                extracted_paths[role] = str(final_out)
            except Exception:
                try:
                    if tmp_out.exists():
                        tmp_out.unlink()
                except OSError:
                    pass
                raise
    return extracted_paths


def download_kws_model(
    target_dir: str | Path,
    *,
    urls: Optional[Tuple[str, ...]] = None,
    expected_sha256: Optional[str] = None,
    timeout: float = 120.0,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Tuple[bool, str]:
    """Download & safely extract Sherpa-ONNX WenetSpeech 3.3M int8 KWS model into `target_dir`."""
    try:
        dest_dir = Path(target_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        existing = resolve_kws_model_files(dest_dir)
        if existing is not None:
            return True, str(dest_dir)
    except OSError as exc:
        return False, f"Filesystem error: {exc}"

    cand_urls = tuple(urls) if urls else DEFAULT_URLS
    env_override = os.environ.get("XIAOZHI_KWS_MODEL_URL", "").strip()
    if env_override:
        cand_urls = (env_override,) + cand_urls
    eff_sha = (expected_sha256 or os.environ.get("XIAOZHI_KWS_MODEL_SHA256", "")).strip().lower()

    opener = urllib.request.build_opener(_SafeRedirectHandler)
    last_err = "no download URLs configured"
    for url in cand_urls:
        tmp_archive = dest_dir / f".{ARCHIVE_NAME}.{os.getpid()}.tmp"
        try:
            clean_url = _validate_url_and_dns(url)
            req = urllib.request.Request(
                clean_url,
                headers={"User-Agent": "xiaozhi-server-kws-downloader/1.0"},
            )
            hasher = hashlib.sha256()
            written = 0
            with opener.open(req, timeout=timeout) as resp:
                final_url = resp.geturl()
                _validate_url_and_dns(final_url)
                total = int(resp.headers.get("Content-Length") or 0)
                with open(tmp_archive, "wb") as out:
                    while True:
                        chunk = resp.read(128 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_EXPECTED_BYTES:
                            raise ValueError("Downloaded archive exceeds safety size cap")
                        out.write(chunk)
                        hasher.update(chunk)
                        if callable(progress_cb):
                            progress_cb(written, total)
                    out.flush()
                    os.fsync(out.fileno())
            if written < MIN_EXPECTED_BYTES:
                raise ValueError(f"Downloaded archive too small ({written} bytes)")
            digest = hasher.hexdigest().lower()
            if eff_sha and digest != eff_sha:
                raise ValueError(f"SHA-256 mismatch: expected {eff_sha}, got {digest}")
            _safe_extract_kws_archive(tmp_archive, dest_dir)
            resolved = resolve_kws_model_files(dest_dir)
            if resolved is None:
                raise ValueError("KWS model files incomplete after extraction")
            return True, str(dest_dir)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if tmp_archive.exists():
                    tmp_archive.unlink()
            except OSError:
                pass

    return False, last_err


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download Sherpa-ONNX WenetSpeech 3.3M int8 Chinese streaming KWS model (~15 MB)"
    )
    default_dir = Path(__file__).resolve().parent.parent / "models" / "sherpa-onnx-kws"
    parser.add_argument("--dir", default=str(default_dir), help="Target directory for KWS ONNX model")
    parser.add_argument("--sha256", default="", help="Optional expected SHA-256 digest of tar.bz2")
    args = parser.parse_args(argv)

    def _prog(done: int, total: int) -> None:
        if total > 0:
            pct = min(100.0, done * 100.0 / total)
            print(f"\rDownloading {ARCHIVE_NAME}: {pct:5.1f}% ({done // 1024} / {total // 1024} KiB)", end="", flush=True)

    ok, detail = download_kws_model(args.dir, expected_sha256=args.sha256 or None, progress_cb=_prog)
    print()
    if ok:
        print(f"[OK] KWS model ready: {detail}")
        return 0
    print(f"[FAIL] KWS model download failed: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
