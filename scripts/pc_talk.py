"""Official zero-hardware Windows PC talk client for XiaoZhi Server (R2 P3-1).

Connects through the standard authenticated flow (18003 `/xiaozhi/ota/` HMAC
token minting -> 18000 `/xiaozhi/v1/` WebSocket) using ONLY Python standard
library modules (zero `pip install` required).

When run on the same Windows host as XiaoZhi Server:
  - Automatically reads `state/.console_token` (if present) to self-register
    its client device ID (`pc-talk-local`) in the allowlist on first run;
  - Automatically ensures audio output target is set to `pc` (48kHz Windows
    speakers via host port `18900`) unless `--keep-audio-target` is passed;
  - Streams assistant subtitles in the terminal while 48kHz voice plays through
    your PC speakers/headphones.

Usage:
  python scripts/pc_talk.py
  python scripts/pc_talk.py --once "你好呀，给我讲个简短的小故事"
  python scripts/pc_talk.py --device-id pc-talk-01 --ota-url http://127.0.0.1:18003/xiaozhi/ota/
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TOKEN_FILE = SERVER_DIR / "state" / ".console_token"
DEFAULT_OTA_URL = "http://127.0.0.1:18003/xiaozhi/ota/"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:18080"
DEFAULT_DEVICE_ID = "pc-talk-local"
DEFAULT_CLIENT_ID = "xiaozhi-pc-talk"

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_loopback_host(host: str | None) -> bool:
    h = (host or "").strip().lower().strip("[]")
    return h in ("127.0.0.1", "localhost", "::1")


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 6.0,
) -> tuple[int, dict[str, Any], str]:
    body_bytes = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method.upper() == "POST" else None
    req_headers = {"Accept": "application/json"}
    if body_bytes is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method.upper())
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as he:
        raw = he.read().decode("utf-8", errors="replace") if he.fp is not None else ""
        status = int(he.code)
    try:
        parsed = json.loads(raw) if raw.strip() else {}
        if not isinstance(parsed, dict):
            parsed = {"data": parsed}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed, raw


def _read_local_console_token(token_file: Path) -> str:
    try:
        if token_file.is_file():
            return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _console_post(console_url: str, path: str, token: str, payload: dict[str, Any], timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    base = console_url.rstrip("/")
    parsed_u = urllib.parse.urlparse(base)
    port = parsed_u.port or 18080
    origin = f"http://127.0.0.1:{port}" if _is_loopback_host(parsed_u.hostname) else base
    status, data, _ = _http_json(
        "POST",
        f"{base}{path}",
        headers={
            "Origin": origin,
            "X-Console-Token": token,
        },
        payload=payload,
        timeout=timeout,
    )
    return status, data


def _connect_ipv4_first(host: str, port: int, timeout: float) -> socket.socket:
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    ordered = sorted(
        infos,
        key=lambda item: 0 if item[0] == socket.AF_INET else (1 if item[0] == socket.AF_INET6 else 2),
    )
    last_exc: Exception | None = None
    for family, socktype, proto, _canon, sockaddr in ordered:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except Exception as exc:
            last_exc = exc
            try:
                sock.close()
            except OSError:
                pass
    raise ConnectionError(f"无法连接 {host}:{port}: {last_exc}")


def encode_ws_client_frame(opcode: int, payload: bytes, mask_key: bytes | None = None) -> bytes:
    """Build a masked RFC 6455 client->server frame."""
    fin_op = 0x80 | (opcode & 0x0F)
    length = len(payload)
    key = mask_key if (mask_key is not None and len(mask_key) == 4) else os.urandom(4)
    header = bytearray([fin_op])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(key)
    masked = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
    return bytes(header) + masked


class MinimalWsClient:
    """Dependency-free RFC 6455 WebSocket client supporting text/binary frames, ping/pong, and buffered upgrades."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 8.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError(f"Unsupported WebSocket URL scheme: {url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw_sock = _connect_ipv4_first(host, port, timeout)
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            raw_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        self._sock = raw_sock
        self._rx_buf = bytearray()
        self._frag_opcode: int | None = None
        self._frag_buf = bytearray()
        self._tx_lock = threading.Lock()
        self._closed = False

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")

        host_hdr = f"{host}:{port}" if parsed.port else host
        req_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_hdr}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Version: 13",
        ]
        for hk, hv in (headers or {}).items():
            req_lines.append(f"{hk}: {hv}")
        req_lines.append("")
        req_lines.append("")
        self._sock.sendall("\r\n".join(req_lines).encode("utf-8"))

        hdr_bytes = self._read_until(b"\r\n\r\n", max_bytes=65536)
        hdr_text = hdr_bytes.decode("latin-1", errors="replace")
        lines = hdr_text.split("\r\n")
        status_line = lines[0] if lines else ""
        if " 101 " not in f" {status_line} " and not status_line.startswith("HTTP/1.1 101"):
            self.close()
            raise ConnectionError(f"WebSocket 握手失败: {status_line}")
        resp_hdrs: dict[str, str] = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                resp_hdrs[k.strip().lower()] = v.strip()
        if resp_hdrs.get("sec-websocket-accept") != expected_accept:
            self.close()
            raise ConnectionError("WebSocket Sec-WebSocket-Accept 校验不匹配")

    def _read_until(self, marker: bytes, *, max_bytes: int) -> bytes:
        while marker not in self._rx_buf:
            if len(self._rx_buf) > max_bytes:
                raise ConnectionError("WebSocket 响应头超出长度上限")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("对端在握手期间关闭了连接")
            self._rx_buf.extend(chunk)
        idx = self._rx_buf.index(marker) + len(marker)
        out = bytes(self._rx_buf[:idx])
        del self._rx_buf[:idx]
        return out

    def _ensure_buffered(self, n: int) -> None:
        while len(self._rx_buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._rx_buf)))
            if not chunk:
                raise EOFError("WebSocket 连接已关闭")
            self._rx_buf.extend(chunk)

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        frame = encode_ws_client_frame(opcode, payload)
        with self._tx_lock:
            if not self._closed:
                self._sock.sendall(frame)

    def send_json(self, obj: dict[str, Any]) -> None:
        self.send_frame(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def recv_message(self, timeout: float | None = None) -> tuple[int, bytes]:
        """Receive one complete reassembled WS message `(opcode, payload)` (`0x1` text, `0x2` binary, `0x8` close)."""
        frame_finish_timeout = None if timeout is None else max(float(timeout), 5.0)
        while True:
            self._sock.settimeout(timeout)
            self._ensure_buffered(2)
            b0 = self._rx_buf[0]
            b1 = self._rx_buf[1]
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            hdr_len = 2
            if length == 126:
                if len(self._rx_buf) < 4:
                    self._sock.settimeout(frame_finish_timeout)
                    self._ensure_buffered(4)
                (length,) = struct.unpack("!H", bytes(self._rx_buf[2:4]))
                hdr_len = 4
            elif length == 127:
                if len(self._rx_buf) < 10:
                    self._sock.settimeout(frame_finish_timeout)
                    self._ensure_buffered(10)
                (length,) = struct.unpack("!Q", bytes(self._rx_buf[2:10]))
                hdr_len = 10

            mask_offset = hdr_len
            if masked:
                hdr_len += 4
            total_frame_len = hdr_len + length
            if len(self._rx_buf) < total_frame_len:
                self._sock.settimeout(frame_finish_timeout)
                self._ensure_buffered(total_frame_len)

            mask_key = bytes(self._rx_buf[mask_offset : mask_offset + 4]) if masked else b""
            payload = bytes(self._rx_buf[hdr_len:total_frame_len]) if length > 0 else b""
            del self._rx_buf[:total_frame_len]

            if masked and payload:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == 0x9:  # Ping -> auto Pong (may interleave mid-fragment per RFC 6455 §5.4)
                self.send_frame(0xA, payload[:125])
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode == 0x8:  # Close
                self._closed = True
                return 0x8, payload
            if opcode == 0x0:
                if self._frag_opcode is None:
                    continue
                self._frag_buf.extend(payload)
            else:
                self._frag_opcode = opcode
                self._frag_buf = bytearray(payload)
            if fin and self._frag_opcode is not None:
                out_op = self._frag_opcode
                out_bytes = bytes(self._frag_buf)
                self._frag_opcode = None
                self._frag_buf = bytearray()
                return out_op, out_bytes

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.send_frame(0x8, b"")
        except Exception:
            pass
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


def negotiate_ota_session(
    *,
    ota_url: str,
    console_url: str,
    device_id: str,
    client_id: str,
    token_file: Path = DEFAULT_TOKEN_FILE,
    auto_allow: bool = True,
    ensure_pc_audio: bool = True,
    explicit_ws_url: str = "",
) -> dict[str, Any]:
    """Authenticate via 18003 OTA (auto-allowlisting via local `state/.console_token` if needed) and return WS info."""
    ota_headers = {"Device-Id": device_id, "Client-Id": client_id}
    status, data, raw = _http_json("POST", ota_url, headers=ota_headers, payload={})
    console_tok = _read_local_console_token(token_file)
    auto_allowed = False

    if status == 403 and auto_allow and console_tok:
        c_status, c_res = _console_post(
            console_url,
            "/api/esp32-devices",
            console_tok,
            {"action": "add", "device_id": device_id},
        )
        if c_status == 200 and c_res.get("ok"):
            auto_allowed = True
            status, data, raw = _http_json("POST", ota_url, headers=ota_headers, payload={})

    if status == 403:
        reported_id = data.get("device_id") or device_id
        raise PermissionError(
            f"设备/客户端 ID `{reported_id}` 尚未加入白名单 (HTTP 403, error={data.get('error', 'forbidden')})。\n"
            f"请打开 {console_url} 顶部「设备与电脑客户端接入白名单」卡片，对 `{reported_id}` 点击「一键放行」后重试。"
        )
    if status != 200:
        raise RuntimeError(f"OTA 握手失败 (HTTP {status}): {raw[:240]}")

    pc_audio_status: dict[str, Any] = {}
    if ensure_pc_audio and console_tok:
        try:
            a_status, a_res = _console_post(console_url, "/api/audio-output", console_tok, {"target": "pc"})
            if a_status == 200 and a_res.get("ok"):
                pc_audio_status = a_res
        except Exception:
            pass

    ws_blk = data.get("websocket") if isinstance(data.get("websocket"), dict) else {}
    ws_token = str(ws_blk.get("token") or "").strip()
    ws_url = explicit_ws_url.strip() or str(ws_blk.get("url") or "").strip()
    if not ws_url:
        ws_url = "ws://127.0.0.1:18000/xiaozhi/v1/"

    # When talking to local 127.0.0.1 OTA, rewrite LAN IP in returned ws_url to 127.0.0.1 to avoid VPN/hairpin issues
    ota_parsed = urllib.parse.urlparse(ota_url)
    if not explicit_ws_url and _is_loopback_host(ota_parsed.hostname):
        ws_parsed = urllib.parse.urlparse(ws_url)
        ws_port = ws_parsed.port or 18000
        ws_url = urllib.parse.urlunparse(
            (ws_parsed.scheme or "ws", f"127.0.0.1:{ws_port}", ws_parsed.path or "/xiaozhi/v1/", "", "", "")
        )

    return {
        "ws_url": ws_url,
        "ws_token": ws_token,
        "device_id": device_id,
        "client_id": client_id,
        "auto_allowed": auto_allowed,
        "pc_audio_status": pc_audio_status,
    }


def open_xiaozhi_ws_session(session_info: dict[str, Any], *, timeout: float = 8.0) -> tuple[MinimalWsClient, str]:
    """Open authenticated 18000 WebSocket, complete `hello` handshake, and return `(ws, session_id)`."""
    hdrs = {
        "Device-Id": str(session_info["device_id"]),
        "Client-Id": str(session_info["client_id"]),
    }
    if session_info.get("ws_token"):
        hdrs["Authorization"] = f"Bearer {session_info['ws_token']}"

    ws = MinimalWsClient(str(session_info["ws_url"]), headers=hdrs, timeout=timeout)
    ws.send_json(
        {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": False},
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
    )
    deadline = time.monotonic() + timeout
    session_id = ""
    while time.monotonic() < deadline:
        op, payload = ws.recv_message(timeout=max(0.5, deadline - time.monotonic()))
        if op == 0x8:
            ws.close()
            raise ConnectionError("WebSocket 在 hello 握手期间被服务端关闭")
        if op != 0x1:
            continue
        txt = payload.decode("utf-8", errors="replace")
        if "认证失败" in txt:
            ws.close()
            raise PermissionError(f"WebSocket 鉴权失败: {txt}")
        try:
            msg = json.loads(txt)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("type") == "hello":
            session_id = str(msg.get("session_id") or "")
            break
    return ws, session_id


def run_single_turn(ws: MinimalWsClient, text: str, *, timeout: float = 45.0) -> list[str]:
    """Send one text utterance via `listen/detect` and stream assistant sentences until `tts/stop`."""
    ws.send_json({"type": "listen", "state": "detect", "text": text})
    sentences: list[str] = []
    opus_frames = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            op, payload = ws.recv_message(timeout=min(2.0, max(0.2, deadline - time.monotonic())))
        except (TimeoutError, socket.timeout):
            continue
        if op == 0x8:
            break
        if op == 0x2:
            opus_frames += 1
            continue
        if op != 0x1:
            continue
        try:
            msg = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if mtype == "tts":
            state = msg.get("state")
            seg_txt = str(msg.get("text") or "").strip()
            if state == "sentence_start" and seg_txt:
                sentences.append(seg_txt)
                print(f"小智 > {seg_txt}", flush=True)
            elif state == "stop":
                if opus_frames > 0 and sentences:
                    print(
                        "  [提示] 当前语音流走到了设备端 Opus 通道；若希望由电脑音箱发声，请在 http://localhost:18080 将输出端切为「Windows 电脑音响」。",
                        flush=True,
                    )
                break
        elif mtype == "llm" and msg.get("emotion"):
            pass
    return sentences


def run_interactive_loop(
    ws: MinimalWsClient,
    *,
    stdin_stream=None,
    turn_timeout: float = 60.0,
) -> int:
    in_stream = stdin_stream if stdin_stream is not None else sys.stdin
    turn_done = threading.Event()
    turn_done.set()
    stop_evt = threading.Event()
    wakeup_evt = threading.Event()
    in_q: queue.Queue[str | None] = queue.Queue()

    def _rx_worker():
        while not stop_evt.is_set():
            try:
                op, payload = ws.recv_message(timeout=1.0)
            except (TimeoutError, socket.timeout):
                continue
            except Exception as exc:
                if not stop_evt.is_set():
                    print(f"\n[连接断开] {exc}", flush=True)
                stop_evt.set()
                turn_done.set()
                wakeup_evt.set()
                break
            if op == 0x8:
                if not stop_evt.is_set():
                    print("\n[提示] 服务端已关闭会话连接（可能触发了空闲超时），重新运行即可继续对话。", flush=True)
                stop_evt.set()
                turn_done.set()
                wakeup_evt.set()
                break
            if op != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "tts":
                st = msg.get("state")
                seg = str(msg.get("text") or "").strip()
                if st in ("start", "sentence_start"):
                    turn_done.clear()
                if st == "sentence_start" and seg:
                    print(f"\r小智 > {seg}", flush=True)
                elif st == "stop":
                    turn_done.set()
                    wakeup_evt.set()

    def _stdin_worker():
        while not stop_evt.is_set():
            try:
                line = in_stream.readline()
            except Exception:
                in_q.put(None)
                wakeup_evt.set()
                break
            if line == "":
                in_q.put(None)
                wakeup_evt.set()
                break
            in_q.put(line.rstrip("\r\n"))
            wakeup_evt.set()

    threading.Thread(target=_rx_worker, name="pc-talk-rx", daemon=True).start()
    threading.Thread(target=_stdin_worker, name="pc-talk-stdin", daemon=True).start()

    print("=========================================================")
    print("  小智电脑端对话客户端已连接！（无需 ESP32 硬件）")
    print("  直接输入文字回车即可对话；输入 /stop 立即打断播报；输入 /quit 退出")
    print("=========================================================")

    try:
        while not stop_evt.is_set():
            if in_q.empty():
                sys.stdout.write("\n你 > ")
                sys.stdout.flush()
            user_line: str | None = None
            got_line = False
            while not stop_evt.is_set():
                try:
                    user_line = in_q.get(timeout=0.5)
                    got_line = True
                    break
                except queue.Empty:
                    continue
            if not got_line or user_line is None:
                break

            text = user_line.strip()
            if not text:
                continue
            low = text.lower()
            if low in ("/quit", "/exit", "exit", "quit"):
                if not turn_done.is_set():
                    try:
                        ws.send_json({"type": "abort", "reason": "wake_word_detected"})
                    except Exception:
                        pass
                break
            if low in ("/stop", "/abort"):
                ws.send_json({"type": "abort", "reason": "wake_word_detected"})
                turn_done.wait(timeout=0.15)
                turn_done.set()
                print("  [已发送打断指令]", flush=True)
                continue

            if not turn_done.is_set():
                ws.send_json({"type": "abort", "reason": "wake_word_detected"})
                turn_done.wait(timeout=0.08)
            turn_done.clear()
            wakeup_evt.clear()
            ws.send_json({"type": "listen", "state": "detect", "text": text})
            # Wait until EITHER the reply completes (`turn_done`) OR the user types `/stop` / barge-in (`in_q`)
            wait_deadline = time.monotonic() + max(0.5, float(turn_timeout))
            while not stop_evt.is_set() and not turn_done.is_set() and in_q.empty():
                rem = wait_deadline - time.monotonic()
                if rem <= 0:
                    turn_done.set()
                    break
                wakeup_evt.wait(timeout=min(0.2, rem))
                wakeup_evt.clear()
    except KeyboardInterrupt:
        print("\n[已退出]", flush=True)
    finally:
        if not turn_done.is_set():
            try:
                ws.send_json({"type": "abort", "reason": "wake_word_detected"})
            except Exception:
                pass
        stop_evt.set()
        wakeup_evt.set()
        ws.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="XiaoZhi official zero-hardware Windows PC talk client")
    parser.add_argument("--ota-url", default=DEFAULT_OTA_URL, help=f"OTA endpoint URL (default: {DEFAULT_OTA_URL})")
    parser.add_argument("--ws-url", default="", help="Optional explicit WebSocket URL override")
    parser.add_argument("--console-url", default=DEFAULT_CONSOLE_URL, help=f"Local Web Console URL (default: {DEFAULT_CONSOLE_URL})")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help=f"Client device ID (default: {DEFAULT_DEVICE_ID})")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help=f"Client instance ID (default: {DEFAULT_CLIENT_ID})")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE), help="Path to local state/.console_token")
    parser.add_argument("--no-auto-allow", action="store_true", help="Do not auto-add device-id via local console token")
    parser.add_argument("--keep-audio-target", action="store_true", help="Do not auto-switch audio output target to PC speakers")
    parser.add_argument("--once", default="", help="Send a single message, wait for reply completion, and exit")
    parser.add_argument("--timeout", type=float, default=45.0, help="Turn timeout in seconds for --once mode")
    args = parser.parse_args(argv)

    try:
        sess = negotiate_ota_session(
            ota_url=args.ota_url,
            console_url=args.console_url,
            device_id=args.device_id,
            client_id=args.client_id,
            token_file=Path(args.token_file),
            auto_allow=not args.no_auto_allow,
            ensure_pc_audio=not args.keep_audio_target,
            explicit_ws_url=args.ws_url,
        )
        if sess.get("auto_allowed"):
            print(f"[自动放行] 已通过本机控制台令牌将 `{sess['device_id']}` 加入白名单", flush=True)
        pc_st = sess.get("pc_audio_status") or {}
        if pc_st and not pc_st.get("pc_audio_online", True):
            print("[提示] 宿主机 18900 电脑音响桥接当前未在线，仅显示文字回复；运行「启动或重启服务.bat」可开启 48kHz 电脑音箱播报。", flush=True)

        ws, _sid = open_xiaozhi_ws_session(sess)
    except Exception as exc:
        print(f"[连接失败] {exc}", file=sys.stderr, flush=True)
        return 1

    if args.once:
        try:
            run_single_turn(ws, args.once, timeout=args.timeout)
        finally:
            ws.close()
        return 0

    return run_interactive_loop(ws)


if __name__ == "__main__":
    raise SystemExit(main())
