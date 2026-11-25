"""Same-origin authenticated WebSocket bridge & one-time session tickets for 18080 PC Web Mic.

Security design:
- `POST /api/pc-mic/session` requires standard `X-Console-Token` + loopback `Origin` (`ConsoleHandler._enforce_security`).
- The upstream `18000` HMAC Bearer token is minted internally inside the container and NEVER sent to browser JS.
- Browser JS only receives a 30-second, single-use opaque ticket (`ws_path = "/ws/pc-mic?ticket=..."`).
- `GET /ws/pc-mic` validates loopback `Origin` (anti-CSWSH), atomically consumes the single-use ticket, opens an
  authenticated loopback WebSocket to `127.0.0.1:<ws_port>/xiaozhi/v1/`, and bridges RFC 6455 frames cleanly.
"""

from __future__ import annotations

import base64
import hashlib
from http import HTTPStatus
import json
import os
import re
import secrets
import socket
import struct
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

import pc_audio_router
from console import shared
from console.routes import config as config_routes
from console.routes import state as state_routes

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{2,64}$")
DEFAULT_PC_MIC_DEVICE_ID = "pc-talk-local"
DEFAULT_PC_MIC_CLIENT_ID = "xiaozhi-web-mic"

_TICKET_TTL_SECONDS = 30.0
_MAX_PENDING_TICKETS = 8
_MAX_CLIENT_FRAME_BYTES = 65536
_MAX_SERVER_FRAME_BYTES = 262144

_ticket_lock = threading.Lock()
_pending_tickets: dict[str, dict[str, Any]] = {}


def _prune_expired_tickets_locked(now: float) -> None:
    expired = [k for k, v in _pending_tickets.items() if float(v.get("expires_at", 0.0)) <= now]
    for k in expired:
        _pending_tickets.pop(k, None)


def issue_mic_ticket(
    *,
    ws_url: str,
    ws_token: str,
    device_id: str,
    client_id: str,
    ttl_seconds: float = _TICKET_TTL_SECONDS,
) -> str:
    """Issue a single-use short-lived opaque ticket bound to internal loopback WS credentials."""
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    with _ticket_lock:
        _prune_expired_tickets_locked(now)
        while len(_pending_tickets) >= _MAX_PENDING_TICKETS:
            oldest_key = next(iter(_pending_tickets))
            _pending_tickets.pop(oldest_key, None)
        _pending_tickets[ticket] = {
            "ws_url": str(ws_url),
            "ws_token": str(ws_token or ""),
            "device_id": str(device_id),
            "client_id": str(client_id),
            "expires_at": now + max(1.0, float(ttl_seconds)),
        }
    return ticket


def consume_mic_ticket(ticket: str) -> dict[str, Any] | None:
    """Atomically pop and validate a single-use ticket, returning None if missing, expired, or reused."""
    tok = str(ticket or "").strip()
    if not tok:
        return None
    now = time.monotonic()
    with _ticket_lock:
        _prune_expired_tickets_locked(now)
        matched_key = None
        for k in list(_pending_tickets.keys()):
            if secrets.compare_digest(k, tok):
                matched_key = k
                break
        if matched_key is None:
            return None
        entry = _pending_tickets.pop(matched_key, None)
    if not entry or float(entry.get("expires_at", 0.0)) <= now:
        return None
    return entry


def clear_mic_tickets() -> None:
    with _ticket_lock:
        _pending_tickets.clear()


def _resolve_internal_ports(cfg: dict[str, Any]) -> tuple[int, int]:
    """Return `(ws_port, http_port)` listening inside this container."""
    srv = cfg.get("server") if isinstance(cfg, dict) else None
    if not isinstance(srv, dict):
        srv = {}
    try:
        ws_port = int(srv.get("port") or 8000)
    except (TypeError, ValueError):
        ws_port = 8000
    try:
        http_port = int(srv.get("http_port") or 8003)
    except (TypeError, ValueError):
        http_port = 8003
    return ws_port, http_port


def _mint_internal_ws_session(
    *,
    device_id: str,
    client_id: str,
    auto_allow: bool = True,
) -> tuple[bool, dict[str, Any] | str]:
    """Negotiate an internal loopback `127.0.0.1:<http_port>/xiaozhi/ota/` token for the browser mic."""
    cfg = shared._read_raw_yaml()
    ws_port, http_port = _resolve_internal_ports(cfg)
    ota_url = f"http://127.0.0.1:{http_port}/xiaozhi/ota/"
    ota_headers = {"Device-Id": device_id, "Client-Id": client_id, "Accept": "application/json"}
    auto_allowed = False

    def _call_ota() -> tuple[int, dict[str, Any], str]:
        with httpx.Client(trust_env=False, timeout=4.0, follow_redirects=False) as client:
            resp = client.post(ota_url, headers=ota_headers, json={})
            raw_txt = resp.text or ""
            try:
                parsed = resp.json() if raw_txt.strip() else {}
                if not isinstance(parsed, dict):
                    parsed = {}
            except ValueError:
                parsed = {}
            return int(resp.status_code), parsed, raw_txt

    try:
        status, data, raw = _call_ota()
    except Exception as exc:
        shared._log_rate_limited("pc_mic_ota_call", exc)
        return False, f"无法连接本地 OTA 鉴权服务 (127.0.0.1:{http_port}): {exc}"

    if status == 403 and auto_allow:
        ok_add, add_res = config_routes._update_esp32_devices({"action": "add", "device_id": device_id})
        if ok_add:
            auto_allowed = True
            try:
                status, data, raw = _call_ota()
            except Exception as exc:
                shared._log_rate_limited("pc_mic_ota_retry", exc)
                return False, f"白名单放行后重试 OTA 失败: {exc}"
        else:
            shared._log_rate_limited("pc_mic_auto_allow", str(add_res))

    if status == 403:
        return False, f"客户端 ID `{device_id}` 不在白名单中，请先在「系统与调优」白名单中放行。"
    if status != 200:
        return False, f"本地 OTA 签发语音令牌失败 (HTTP {status}): {raw[:160]}"

    ws_blk = data.get("websocket") if isinstance(data.get("websocket"), dict) else {}
    ws_token = str(ws_blk.get("token") or "").strip()
    ws_path = "/xiaozhi/v1/"
    raw_ws_url = str(ws_blk.get("url") or "").strip()
    if raw_ws_url:
        parsed_ws = urlparse(raw_ws_url)
        if parsed_ws.path:
            ws_path = parsed_ws.path
    internal_ws_url = f"ws://127.0.0.1:{ws_port}{ws_path}"
    return True, {
        "ws_url": internal_ws_url,
        "ws_token": ws_token,
        "device_id": device_id,
        "client_id": client_id,
        "auto_allowed": auto_allowed,
    }


def handle_post_pc_mic_session(handler) -> None:
    """POST /api/pc-mic/session -> mint one-time same-origin WS bridge ticket."""
    try:
        body = handler._read_body_json(max_bytes=16 * 1024)
    except shared._BadRequestError:
        raise
    except Exception as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return

    dev_raw = str(body.get("device_id") or DEFAULT_PC_MIC_DEVICE_ID).strip().lower()
    if not _DEVICE_ID_RE.match(dev_raw):
        handler._send_json({"ok": False, "error": "无效的客户端设备 ID 格式"}, HTTPStatus.BAD_REQUEST)
        return
    client_raw = str(body.get("client_id") or DEFAULT_PC_MIC_CLIENT_ID).strip()
    if not _DEVICE_ID_RE.match(client_raw):
        client_raw = DEFAULT_PC_MIC_CLIENT_ID

    auto_allow = bool(body.get("auto_allow", True))
    ensure_pc_audio = bool(body.get("ensure_pc_audio", True))

    ok, res_or_err = _mint_internal_ws_session(
        device_id=dev_raw,
        client_id=client_raw,
        auto_allow=auto_allow,
    )
    if not ok:
        handler._send_json({"ok": False, "error": str(res_or_err)}, HTTPStatus.BAD_GATEWAY)
        return

    info = res_or_err
    audio_cfg = pc_audio_router.read_audio_output_config()
    audio_target = str(audio_cfg.get("target") or "pc")
    pc_online = pc_audio_router.is_pc_audio_bridge_online(force=True)
    state_routes._set_cached_pc_audio_online(pc_online)
    if ensure_pc_audio and pc_online and audio_target != "pc":
        try:
            saved = pc_audio_router.write_audio_output_config("pc", None)
            audio_target = str(saved.get("target") or "pc")
        except Exception as exc:
            shared._log_rate_limited("pc_mic_switch_audio", exc)

    ticket = issue_mic_ticket(
        ws_url=info["ws_url"],
        ws_token=info["ws_token"],
        device_id=info["device_id"],
        client_id=info["client_id"],
    )
    handler._send_json(
        {
            "ok": True,
            "ws_path": f"/ws/pc-mic?ticket={ticket}",
            "device_id": info["device_id"],
            "auto_allowed": bool(info.get("auto_allowed", False)),
            "pc_audio_online": bool(pc_online),
            "audio_target": audio_target,
            "sample_rate": 16000,
            "frame_ms": 60,
        }
    )


def _encode_ws_frame(opcode: int, payload: bytes, *, masked: bool) -> bytes:
    fin_op = 0x80 | (opcode & 0x0F)
    length = len(payload)
    mask_bit = 0x80 if masked else 0x00
    header = bytearray([fin_op])
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if masked:
        key = os.urandom(4)
        header.extend(key)
        body = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
        return bytes(header) + body
    return bytes(header) + payload


class _WsSocketReader:
    """Minimal buffered RFC 6455 frame reader over a raw TCP socket."""

    def __init__(self, sock: socket.socket, *, require_masked: bool, max_payload: int):
        self._sock = sock
        self._buf = bytearray()
        self._require_masked = bool(require_masked)
        self._max_payload = max(1024, int(max_payload))
        self._frag_opcode: int | None = None
        self._frag_buf = bytearray()

    def _fill(self, n: int, stop_evt: threading.Event) -> None:
        while len(self._buf) < n:
            if stop_evt.is_set():
                raise EOFError("bridge stopped")
            try:
                chunk = self._sock.recv(max(4096, n - len(self._buf)))
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                raise EOFError("socket closed")
            self._buf.extend(chunk)

    def read_message(self, stop_evt: threading.Event) -> tuple[int, bytes]:
        while not stop_evt.is_set():
            self._fill(2, stop_evt)
            b0 = self._buf[0]
            b1 = self._buf[1]
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if self._require_masked and not masked:
                raise ValueError("unmasked client WebSocket frame rejected")
            hdr_len = 2
            if length == 126:
                self._fill(4, stop_evt)
                (length,) = struct.unpack("!H", bytes(self._buf[2:4]))
                hdr_len = 4
            elif length == 127:
                self._fill(10, stop_evt)
                (length,) = struct.unpack("!Q", bytes(self._buf[2:10]))
                hdr_len = 10
            if length > self._max_payload:
                raise ValueError(f"WebSocket frame too large ({length} > {self._max_payload})")
            mask_offset = hdr_len
            if masked:
                hdr_len += 4
            total = hdr_len + length
            self._fill(total, stop_evt)
            mask_key = bytes(self._buf[mask_offset : mask_offset + 4]) if masked else b""
            payload = bytes(self._buf[hdr_len:total]) if length > 0 else b""
            del self._buf[:total]
            if masked and payload:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode in (0x8, 0x9, 0xA):
                return opcode, payload
            if opcode == 0x0:
                if self._frag_opcode is None:
                    continue
                if len(self._frag_buf) + len(payload) > self._max_payload:
                    raise ValueError("fragmented WebSocket message exceeds limit")
                self._frag_buf.extend(payload)
            else:
                self._frag_opcode = opcode
                self._frag_buf = bytearray(payload)
            if fin and self._frag_opcode is not None:
                out_op = self._frag_opcode
                out_payload = bytes(self._frag_buf)
                self._frag_opcode = None
                self._frag_buf = bytearray()
                return out_op, out_payload
        return 0x8, b""


def _open_upstream_ws_socket(
    ws_url: str,
    *,
    device_id: str,
    client_id: str,
    ws_token: str,
    timeout: float = 5.0,
) -> tuple[socket.socket, bytearray]:
    """Perform a loopback RFC 6455 client handshake to `ws://127.0.0.1:<port>/xiaozhi/v1/`."""
    parsed = urlparse(ws_url)
    host = (parsed.hostname or "127.0.0.1").strip()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"Refusing non-loopback upstream WS host: {host}")
    port = int(parsed.port or 8000)
    path = parsed.path or "/xiaozhi/v1/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        req_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Version: 13",
            f"Device-Id: {device_id}",
            f"Client-Id: {client_id}",
        ]
        if ws_token:
            req_lines.append(f"Authorization: Bearer {ws_token}")
        req_lines.extend(["", ""])
        sock.sendall("\r\n".join(req_lines).encode("utf-8"))

        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            if len(buf) > 32768:
                raise ConnectionError("upstream WS handshake header too large")
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("upstream WS closed during handshake")
            buf.extend(chunk)
        idx = buf.index(b"\r\n\r\n") + 4
        hdr_text = bytes(buf[:idx]).decode("latin-1", errors="replace")
        del buf[:idx]
        lines = hdr_text.split("\r\n")
        status_line = lines[0] if lines else ""
        if " 101 " not in f" {status_line} " and not status_line.startswith("HTTP/1.1 101"):
            raise ConnectionError(f"upstream WS handshake rejected: {status_line}")
        resp_hdrs: dict[str, str] = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                resp_hdrs[k.strip().lower()] = v.strip()
        if resp_hdrs.get("sec-websocket-accept") != expected_accept:
            raise ConnectionError("upstream WS Sec-WebSocket-Accept mismatch")
        sock.settimeout(1.0)
        return sock, buf
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _close_sock_quiet(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _bridge_ws_sockets(
    browser_sock: socket.socket,
    upstream_sock: socket.socket,
    upstream_initial_buf: bytearray,
) -> None:
    """Bidirectionally forward RFC 6455 frames between browser (unmasked server) and upstream (masked client)."""
    stop_evt = threading.Event()
    browser_tx_lock = threading.Lock()
    upstream_tx_lock = threading.Lock()
    browser_sock.settimeout(1.0)
    upstream_sock.settimeout(1.0)

    def _send_browser(op: int, payload: bytes) -> None:
        frame = _encode_ws_frame(op, payload, masked=False)
        with browser_tx_lock:
            browser_sock.sendall(frame)

    def _send_upstream(op: int, payload: bytes) -> None:
        frame = _encode_ws_frame(op, payload, masked=True)
        with upstream_tx_lock:
            upstream_sock.sendall(frame)

    def _upstream_to_browser() -> None:
        reader = _WsSocketReader(upstream_sock, require_masked=False, max_payload=_MAX_SERVER_FRAME_BYTES)
        if upstream_initial_buf:
            reader._buf.extend(upstream_initial_buf)
        try:
            while not stop_evt.is_set():
                op, payload = reader.read_message(stop_evt)
                if op == 0x9:
                    _send_upstream(0xA, payload[:125])
                    continue
                if op == 0xA:
                    continue
                if op == 0x8:
                    try:
                        _send_browser(0x8, payload[:125])
                    except OSError:
                        pass
                    break
                if op in (0x1, 0x2):
                    _send_browser(op, payload)
        except (EOFError, OSError, ValueError) as exc:
            if not stop_evt.is_set():
                shared._log_rate_limited("pc_mic_ws_up_to_browser", exc)
        finally:
            stop_evt.set()
            _close_sock_quiet(browser_sock)

    def _browser_to_upstream() -> None:
        reader = _WsSocketReader(browser_sock, require_masked=True, max_payload=_MAX_CLIENT_FRAME_BYTES)
        try:
            while not stop_evt.is_set():
                op, payload = reader.read_message(stop_evt)
                if op == 0x9:
                    _send_browser(0xA, payload[:125])
                    continue
                if op == 0xA:
                    continue
                if op == 0x8:
                    try:
                        _send_upstream(0x8, payload[:125])
                    except OSError:
                        pass
                    break
                if op in (0x1, 0x2):
                    _send_upstream(op, payload)
        except (EOFError, OSError, ValueError) as exc:
            if not stop_evt.is_set():
                shared._log_rate_limited("pc_mic_ws_browser_to_up", exc)
        finally:
            stop_evt.set()
            _close_sock_quiet(upstream_sock)

    t_up = threading.Thread(target=_upstream_to_browser, name="pc-mic-ws-down", daemon=True)
    t_up.start()
    try:
        _browser_to_upstream()
    finally:
        stop_evt.set()
        _close_sock_quiet(upstream_sock)
        _close_sock_quiet(browser_sock)
        t_up.join(timeout=2.0)


def handle_ws_pc_mic(handler, parsed) -> None:
    """GET /ws/pc-mic?ticket=... -> strict Origin + single-use ticket RFC 6455 upgrade & bridge."""
    origin = str(handler.headers.get("Origin") or "").strip().lower()
    if origin and origin not in shared.get_allowed_console_origins(shared.CONSOLE_PORT):
        handler._send_json(
            {"ok": False, "error": "Cross-origin WebSocket upgrade rejected"},
            HTTPStatus.FORBIDDEN,
            close_connection=True,
        )
        return

    upgrade = str(handler.headers.get("Upgrade") or "").strip().lower()
    ws_key = str(handler.headers.get("Sec-WebSocket-Key") or "").strip()
    if upgrade != "websocket" or not ws_key:
        handler._send_json(
            {"ok": False, "error": "Expected RFC 6455 WebSocket Upgrade headers"},
            HTTPStatus.BAD_REQUEST,
            close_connection=True,
        )
        return

    qs = parse_qs(parsed.query or "")
    ticket = (qs.get("ticket") or [""])[0].strip()
    session = consume_mic_ticket(ticket)
    if session is None:
        handler._send_json(
            {"ok": False, "error": "无效、过期或已使用的语音会话票据"},
            HTTPStatus.FORBIDDEN,
            close_connection=True,
        )
        return

    try:
        upstream_sock, upstream_initial_buf = _open_upstream_ws_socket(
            session["ws_url"],
            device_id=session["device_id"],
            client_id=session["client_id"],
            ws_token=session["ws_token"],
        )
    except Exception as exc:
        shared._log_rate_limited("pc_mic_ws_upstream_connect", exc)
        handler._send_json(
            {"ok": False, "error": f"连接语音主服务失败: {exc}"},
            HTTPStatus.BAD_GATEWAY,
            close_connection=True,
        )
        return

    accept_val = base64.b64encode(hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
    resp_bytes = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_val}\r\n"
        "Cache-Control: no-store\r\n"
        "\r\n"
    ).encode("ascii")
    handler.close_connection = True
    try:
        handler.wfile.write(resp_bytes)
        handler.wfile.flush()
    except OSError as exc:
        shared._log_rate_limited("pc_mic_ws_101_write", exc)
        _close_sock_quiet(upstream_sock)
        return

    _bridge_ws_sockets(handler.connection, upstream_sock, upstream_initial_buf)
