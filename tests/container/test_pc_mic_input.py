"""Offline container tests for Patch #23 (`pc_pcm_audio_input`) & 18080 same-origin Web Mic WS bridge."""

from __future__ import annotations

import base64
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT_DIR = "/opt/xiaozhi-esp32-server"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.environ.get("XIAOZHI_APP_DIR") or (
    os.path.join(ROOT_DIR, "app")
    if os.path.isdir(os.path.join(ROOT_DIR, "app"))
    else os.path.join(_REPO_ROOT, "app")
)
for _p in (ROOT_DIR, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx

from console import server as console_server
from console import shared as console_shared
from console.routes import pc_mic as pc_mic_routes
from patches import pc_pcm_input


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_client_frame(opcode: int, payload: bytes) -> bytes:
    return pc_mic_routes._encode_ws_frame(opcode, payload, masked=True)


def _ws_server_frame(opcode: int, payload: bytes) -> bytes:
    return pc_mic_routes._encode_ws_frame(opcode, payload, masked=False)


class TestPcMicInputAndBridge(unittest.TestCase):
    def setUp(self) -> None:
        pc_mic_routes.clear_mic_tickets()

    def tearDown(self) -> None:
        pc_mic_routes.clear_mic_tickets()

    def test_01_patch_pc_pcm_audio_input_passthrough_and_opus_preservation(self):
        """Patch #23 passes valid 16-bit PCM straight through for `audio_format=='pcm'` and keeps Opus untouched."""
        from core.connection import ConnectionHandler

        pc_pcm_input.install_pc_pcm_input_patch()
        self.assertTrue(getattr(ConnectionHandler, "_pc_pcm_input_patched", False))

        # PCM connection: valid 1920-byte frame bypasses opuslib_next.Decoder
        pcm_conn = object.__new__(ConnectionHandler)
        pcm_conn.audio_format = "pcm"
        raw_pcm = b"\x01\x02" * 960
        self.assertEqual(pcm_conn._decode_opus_packet(raw_pcm), raw_pcm)

        # Invalid PCM frames rejected cleanly
        self.assertIsNone(pcm_conn._decode_opus_packet(b""))
        self.assertIsNone(pcm_conn._decode_opus_packet(b"\x01"))
        self.assertIsNone(pcm_conn._decode_opus_packet(b"\x00\x01\x02"))  # odd length
        self.assertIsNone(pcm_conn._decode_opus_packet(b"\x00\x01" * 20000))  # > 32768 bytes

        # Opus connection: still goes through Opus decoder (never returns raw bytes unchanged)
        opus_conn = object.__new__(ConnectionHandler)
        opus_conn.audio_format = "opus"

        class _FakeDecoder:
            def __init__(self):
                self.called_with = None

            def decode(self, pkt, frame_size):
                self.called_with = (pkt, frame_size)
                return b"\x11\x22" * frame_size

        fake_dec = _FakeDecoder()
        opus_conn._connection_opus_decoder = fake_dec
        decoded = opus_conn._decode_opus_packet(b"\xaa\xbb\xcc")
        self.assertEqual(fake_dec.called_with, (b"\xaa\xbb\xcc", 960))
        self.assertEqual(len(decoded), 1920)

    def test_02_ticket_single_use_ttl_and_session_endpoint_security(self):
        """Tickets are single-use + TTL-bound; POST /api/pc-mic/session enforces auth and never leaks ws_token."""
        t1 = pc_mic_routes.issue_mic_ticket(
            ws_url="ws://127.0.0.1:8000/xiaozhi/v1/",
            ws_token="super-secret-upstream-token",
            device_id="pc-talk-local",
            client_id="xiaozhi-web-mic",
            ttl_seconds=30.0,
        )
        got = pc_mic_routes.consume_mic_ticket(t1)
        self.assertIsNotNone(got)
        self.assertEqual(got["ws_token"], "super-secret-upstream-token")
        # Single-use replay rejected
        self.assertIsNone(pc_mic_routes.consume_mic_ticket(t1))

        # Expired ticket rejected
        t_exp = pc_mic_routes.issue_mic_ticket(
            ws_url="ws://127.0.0.1:8000/xiaozhi/v1/",
            ws_token="tok",
            device_id="pc-talk-local",
            client_id="xiaozhi-web-mic",
            ttl_seconds=1.0,
        )
        with pc_mic_routes._ticket_lock:
            pc_mic_routes._pending_tickets[t_exp]["expires_at"] = time.monotonic() - 1.0
        self.assertIsNone(pc_mic_routes.consume_mic_ticket(t_exp))

        # Live ConsoleHandler HTTP checks
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), console_server.ConsoleHandler)
        port = httpd.server_address[1]
        orig_port = console_shared.CONSOLE_PORT
        console_shared.CONSOLE_PORT = port
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{port}"
            tok = console_shared.get_console_token()
            hdrs = {
                "Origin": base,
                "X-Console-Token": tok,
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                # 1. Missing X-Console-Token rejected 401
                r_unauth = client.post(f"{base}/api/pc-mic/session", json={}, headers={"Origin": base})
                self.assertEqual(r_unauth.status_code, 401)

                # 2. Invalid device_id rejected 400
                r_bad = client.post(
                    f"{base}/api/pc-mic/session",
                    json={"device_id": "../evil"},
                    headers=hdrs,
                )
                self.assertEqual(r_bad.status_code, 400)

                # 3. Valid session returns opaque ws_path and NEVER returns ws_token
                fake_mint = (
                    True,
                    {
                        "ws_url": "ws://127.0.0.1:8000/xiaozhi/v1/",
                        "ws_token": "SECRET_HMAC_DO_NOT_EXPOSE",
                        "device_id": "pc-talk-local",
                        "client_id": "xiaozhi-web-mic",
                        "auto_allowed": True,
                    },
                )
                with (
                    patch.object(pc_mic_routes, "_mint_internal_ws_session", return_value=fake_mint),
                    patch.object(pc_mic_routes.pc_audio_router, "is_pc_audio_bridge_online", return_value=True),
                    patch.object(
                        pc_mic_routes.pc_audio_router,
                        "read_audio_output_config",
                        return_value={"target": "pc", "volume": 100},
                    ),
                ):
                    r_ok = client.post(f"{base}/api/pc-mic/session", json={}, headers=hdrs)
                self.assertEqual(r_ok.status_code, 200)
                body = r_ok.json()
                self.assertTrue(body["ok"])
                self.assertTrue(body["ws_path"].startswith("/ws/pc-mic?ticket="))
                self.assertNotIn("SECRET_HMAC_DO_NOT_EXPOSE", r_ok.text)
                self.assertNotIn("ws_token", body)

                # 4. Worklet static asset & CSP loopback WS allowlist
                r_wk = client.get(f"{base}/static/pcm_worklet.js")
                self.assertEqual(r_wk.status_code, 200)
                self.assertIn("xiaozhi-pcm-capture", r_wk.text)
                csp = r_wk.headers.get("Content-Security-Policy", "")
                self.assertIn("script-src 'self'", csp)
                self.assertIn("object-src 'none'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertIn(f"ws://127.0.0.1:{port}", csp)
        finally:
            console_shared.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()

    def test_03_same_origin_ws_bridge_end_to_end_and_cswsh_rejection(self):
        """GET /ws/pc-mic blocks cross-origin & replay, upgrades valid tickets, and bridges JSON + PCM binary."""
        upstream_received: dict = {"headers": {}, "messages": []}
        upstream_ready = threading.Event()
        upstream_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream_srv.bind(("127.0.0.1", 0))
        upstream_srv.listen(2)
        upstream_port = upstream_srv.getsockname()[1]

        def _fake_upstream():
            upstream_ready.set()
            upstream_srv.settimeout(5.0)
            try:
                conn, _ = upstream_srv.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(4.0)
                buf = bytearray()
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf.extend(chunk)
                idx = buf.index(b"\r\n\r\n") + 4
                hdr_txt = bytes(buf[:idx]).decode("latin-1")
                del buf[:idx]
                hdrs = {}
                for ln in hdr_txt.split("\r\n")[1:]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        hdrs[k.strip().lower()] = v.strip()
                upstream_received["headers"] = hdrs
                acc = base64.b64encode(
                    hashlib.sha1((hdrs["sec-websocket-key"] + _WS_GUID).encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {acc}\r\n\r\n"
                    ).encode("ascii")
                )
                stop_e = threading.Event()
                reader = pc_mic_routes._WsSocketReader(conn, require_masked=True, max_payload=65536)
                reader._buf.extend(buf)
                # Expect 1 text hello + 1 binary 1920-byte PCM frame
                for _ in range(2):
                    op, payload = reader.read_message(stop_e)
                    upstream_received["messages"].append((op, payload))
                    if op == 0x1:
                        conn.sendall(
                            _ws_server_frame(0x1, json.dumps({"type": "hello", "session_id": "sid-1"}).encode("utf-8"))
                        )
                    elif op == 0x2:
                        conn.sendall(
                            _ws_server_frame(
                                0x1,
                                json.dumps({"type": "stt", "text": f"pcm_len={len(payload)}"}).encode("utf-8"),
                            )
                        )
                conn.sendall(_ws_server_frame(0x8, b""))

        t_up = threading.Thread(target=_fake_upstream, daemon=True)
        t_up.start()
        upstream_ready.wait(timeout=3.0)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), console_server.ConsoleHandler)
        port = httpd.server_address[1]
        orig_port = console_shared.CONSOLE_PORT
        console_shared.CONSOLE_PORT = port
        t_http = threading.Thread(target=httpd.serve_forever, daemon=True)
        t_http.start()
        try:
            ticket = pc_mic_routes.issue_mic_ticket(
                ws_url=f"ws://127.0.0.1:{upstream_port}/xiaozhi/v1/",
                ws_token="internal-hmac-token-xyz",
                device_id="pc-talk-local",
                client_id="xiaozhi-web-mic",
            )

            # 1. Cross-origin WebSocket upgrade must be rejected 403 WITHOUT consuming ticket
            with socket.create_connection(("127.0.0.1", port), timeout=3.0) as bad_sock:
                ws_k = base64.b64encode(os.urandom(16)).decode("ascii")
                req = (
                    f"GET /ws/pc-mic?ticket={ticket} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "Origin: https://evil.example\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {ws_k}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
                bad_sock.sendall(req)
                resp = bad_sock.recv(1024).decode("latin-1", errors="replace")
                self.assertIn("403", resp.splitlines()[0])

            # 2. Valid same-origin WebSocket upgrade succeeds and bridges JSON + binary PCM
            with socket.create_connection(("127.0.0.1", port), timeout=4.0) as cli_sock:
                cli_sock.settimeout(4.0)
                ws_k = base64.b64encode(os.urandom(16)).decode("ascii")
                req = (
                    f"GET /ws/pc-mic?ticket={ticket} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Origin: http://127.0.0.1:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {ws_k}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
                cli_sock.sendall(req)
                rbuf = bytearray()
                while b"\r\n\r\n" not in rbuf:
                    rbuf.extend(cli_sock.recv(4096))
                hdr_end = rbuf.index(b"\r\n\r\n") + 4
                hdr_lines = bytes(rbuf[:hdr_end]).decode("latin-1").split("\r\n")
                del rbuf[:hdr_end]
                self.assertIn("101", hdr_lines[0])

                # Send hello (pcm) + 1920-byte PCM frame
                cli_sock.sendall(
                    _ws_client_frame(
                        0x1,
                        json.dumps({"type": "hello", "audio_params": {"format": "pcm", "sample_rate": 16000}}).encode(
                            "utf-8"
                        ),
                    )
                )
                pcm_frame = b"\x05\x00" * 960
                cli_sock.sendall(_ws_client_frame(0x2, pcm_frame))

                reader = pc_mic_routes._WsSocketReader(cli_sock, require_masked=False, max_payload=65536)
                reader._buf.extend(rbuf)
                stop_e = threading.Event()
                op1, p1 = reader.read_message(stop_e)
                op2, p2 = reader.read_message(stop_e)
                self.assertEqual(op1, 0x1)
                self.assertEqual(json.loads(p1.decode("utf-8"))["type"], "hello")
                self.assertEqual(op2, 0x1)
                self.assertEqual(json.loads(p2.decode("utf-8"))["text"], "pcm_len=1920")

            # Verify upstream received authentic internal Bearer token + Device-Id + PCM bytes
            self.assertEqual(upstream_received["headers"].get("authorization"), "Bearer internal-hmac-token-xyz")
            self.assertEqual(upstream_received["headers"].get("device-id"), "pc-talk-local")
            self.assertEqual(upstream_received["messages"][1], (0x2, pcm_frame))

            # 3. Ticket replay must be rejected 403
            with socket.create_connection(("127.0.0.1", port), timeout=3.0) as rep_sock:
                ws_k = base64.b64encode(os.urandom(16)).decode("ascii")
                req = (
                    f"GET /ws/pc-mic?ticket={ticket} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Origin: http://127.0.0.1:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {ws_k}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
                rep_sock.sendall(req)
                resp = rep_sock.recv(1024).decode("latin-1", errors="replace")
                self.assertIn("403", resp.splitlines()[0])
        finally:
            console_shared.CONSOLE_PORT = orig_port
            httpd.shutdown()
            httpd.server_close()
            upstream_srv.close()
            t_up.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
