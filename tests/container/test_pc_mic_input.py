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

import asyncio

try:
    import httpx
    import runtime_events
    from console import server as console_server
    from console import shared as console_shared
    from console.routes import pc_mic as pc_mic_routes
    from patches import pc_pcm_input
except ImportError:  # Host-only Node.js VM execution without container deps
    httpx = None
    runtime_events = None
    console_server = None
    console_shared = None
    pc_mic_routes = None
    pc_pcm_input = None


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_client_frame(opcode: int, payload: bytes) -> bytes:
    return pc_mic_routes._encode_ws_frame(opcode, payload, masked=True)


def _ws_server_frame(opcode: int, payload: bytes) -> bytes:
    return pc_mic_routes._encode_ws_frame(opcode, payload, masked=False)


class TestPcMicInputAndBridge(unittest.TestCase):
    def setUp(self) -> None:
        if pc_mic_routes is not None:
            pc_mic_routes.clear_mic_tickets()
        if runtime_events is not None:
            runtime_events.reset_runtime_events()

    def tearDown(self) -> None:
        if pc_mic_routes is not None:
            pc_mic_routes.clear_mic_tickets()
        if runtime_events is not None:
            runtime_events.reset_runtime_events()

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

    def test_04_pc_mic_exclusive_preempts_hardware_and_suppresses_wakeup_and_abort(self):
        """Active PC mic preempts online hardware, silently yields new hardware wakeups, drops Opus, and guards PC speaker."""
        import core.handle.abortHandle as abort_mod
        import core.handle.helloHandle as hello_mod
        import core.handle.textHandler.listenMessageHandler as listen_mod
        from core.connection import ConnectionHandler
        import mei_emotion_router
        import pc_audio_router

        pc_audio_router.apply_pc_audio_playback_patches()
        mei_emotion_router.apply_mei_wakeup_responses_patch()
        pc_pcm_input.install_pc_pcm_input_patch()

        class _FakeWs:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.closed = False

            async def send(self, msg: str) -> None:
                self.sent.append(msg)

            async def close(self) -> None:
                self.closed = True

        class _FakeDecoder:
            def __init__(self) -> None:
                self.calls = 0

            def decode(self, pkt: bytes, frame_size: int) -> bytes:
                self.calls += 1
                return b"\x11\x22" * frame_size

        class _NoopLogger:
            def bind(self, **_kwargs):
                return self

            def debug(self, *_args, **_kwargs):
                return None

            def info(self, *_args, **_kwargs):
                return None

            def warning(self, *_args, **_kwargs):
                return None

            def error(self, *_args, **_kwargs):
                return None

        def _make_conn(device_id: str, audio_format: str, loop) -> ConnectionHandler:
            c = object.__new__(ConnectionHandler)
            c.device_id = device_id
            c.audio_format = audio_format
            c.loop = loop
            c.logger = _NoopLogger()
            c.websocket = _FakeWs()
            c.stop_event = threading.Event()
            c.client_abort = False
            c.client_is_speaking = False
            c.client_have_voice = False
            c.client_voice_stop = False
            c.client_listen_mode = "auto"
            c.asr = None
            c.asr_audio = []
            c.audio_buffer = []
            c.tts = None
            c.config = {"wakeup_words": ["你好芽衣"], "enable_wakeup_words_response_cache": True}
            c.welcome_msg = {
                "type": "hello",
                "transport": "websocket",
                "session_id": f"sid-{device_id}",
                "audio_params": {"format": "opus", "sample_rate": 16000},
            }
            c._reset_count = 0
            c.reset_audio_states = lambda: setattr(c, "_reset_count", c._reset_count + 1)
            c.clear_queues = lambda: None
            c._connection_opus_decoder = _FakeDecoder()
            return c

        async def _scenario() -> None:
            loop = asyncio.get_running_loop()
            listen_handler = listen_mod.ListenTextMessageHandler()
            stop_pc_calls: list[str] = []

            async def _fake_post_stop_to_pc() -> None:
                stop_pc_calls.append("stop")

            # 1. Existing hardware conn speaking on PC speaker + PC text-only conn (not mic-active yet)
            hw_existing = _make_conn("aa:bb:cc:dd:ee:01", "opus", loop)
            hw_existing._pc_audio_active_sid = "hw-old-sid"
            pc_conn = _make_conn("pc-talk-local", "pcm", loop)
            runtime_events.register_connection(hw_existing)
            runtime_events.register_connection(pc_conn)

            # Text-only PC conn does NOT suppress hardware
            self.assertFalse(pc_pcm_input.has_active_pc_mic_connection())
            self.assertIsNotNone(hw_existing._decode_opus_packet(b"\xaa\xbb"))
            self.assertEqual(hw_existing._connection_opus_decoder.calls, 1)

            with (
                patch.object(pc_audio_router, "_post_stop_to_pc", side_effect=_fake_post_stop_to_pc),
                patch.object(
                    pc_audio_router,
                    "read_audio_output_config",
                    return_value={"target": "pc", "volume": 100},
                ),
            ):
                # 2. PC mic starts listening -> preempts existing hardware and stops old hardware PC audio
                await listen_handler.handle(pc_conn, {"type": "listen", "state": "start", "mode": "auto"})
                await asyncio.sleep(0.05)

                self.assertTrue(pc_pcm_input.is_pc_mic_connection_active(pc_conn))
                self.assertTrue(getattr(hw_existing, "_yielded_to_pc_mic", False))
                self.assertTrue(hw_existing.websocket.closed)
                self.assertEqual(len(stop_pc_calls), 1)
                runtime_events.unregister_connection(hw_existing)

                # 3. New hardware wakes up on "你好芽衣" while PC mic is speaking on PC speakers
                pc_conn._pc_audio_active_sid = "pc-live-sid"
                pc_conn.client_is_speaking = True
                hw_wakeup = _make_conn("aa:bb:cc:dd:ee:01", "opus", loop)
                runtime_events.register_connection(hw_wakeup)

                await hello_mod.handleHelloMessage(
                    hw_wakeup,
                    {
                        "type": "hello",
                        "features": {"mcp": True},
                        "audio_params": {"format": "opus", "sample_rate": 16000},
                    },
                )
                # Sends clean welcome_msg (no error beep on ESP32) and skips MCP init
                self.assertEqual(len(hw_wakeup.websocket.sent), 1)
                hello_reply = json.loads(hw_wakeup.websocket.sent[0])
                self.assertEqual(hello_reply["type"], "hello")
                self.assertTrue(getattr(hw_wakeup, "_yielded_to_pc_mic", False))
                self.assertFalse(hasattr(hw_wakeup, "mcp_client"))

                # Stray Opus frame dropped BEFORE decoder
                self.assertIsNone(hw_wakeup._decode_opus_packet(b"\xcc\xdd"))
                self.assertEqual(hw_wakeup._connection_opus_decoder.calls, 0)

                # Stray abort from suppressed hardware NEVER stops PC mic's speaker playback
                await abort_mod.handleAbortMessage(hw_wakeup)
                self.assertEqual(len(stop_pc_calls), 1)

                # Wakeup listen/detect silently dismissed & closes hardware WS without greeting TTS
                await listen_handler.handle(
                    hw_wakeup,
                    {"type": "listen", "state": "detect", "text": "你好芽衣"},
                )
                self.assertTrue(hw_wakeup.websocket.closed)
                self.assertEqual(len(stop_pc_calls), 1)
                self.assertTrue(await hello_mod.checkWakeupWords(hw_wakeup, "你好芽衣"))
                runtime_events.unregister_connection(hw_wakeup)

                # 4. Web PTT release (`listen stop` without `pc_mic_state="off"`) keeps PC mic exclusive
                await listen_handler.handle(pc_conn, {"type": "listen", "state": "stop"})
                self.assertTrue(pc_pcm_input.has_active_pc_mic_connection())

                # 5. Env escape hatch `XIAOZHI_PC_MIC_EXCLUSIVE=0` disables suppression
                with patch.dict(os.environ, {"XIAOZHI_PC_MIC_EXCLUSIVE": "0"}):
                    hw_multiroom = _make_conn("aa:bb:cc:dd:ee:02", "opus", loop)
                    self.assertFalse(pc_pcm_input.should_suppress_hardware_conn(hw_multiroom))
                    self.assertIsNotNone(hw_multiroom._decode_opus_packet(b"\x12\x34"))

                # 6. Terminal `/mic` off (`pc_mic_state="off"`) releases lock immediately without ghost ASR
                await listen_handler.handle(
                    pc_conn,
                    {"type": "listen", "state": "stop", "pc_mic_state": "off"},
                )
                self.assertFalse(pc_pcm_input.has_active_pc_mic_connection())
                hw_resumed = _make_conn("aa:bb:cc:dd:ee:01", "opus", loop)
                self.assertFalse(pc_pcm_input.should_suppress_hardware_conn(hw_resumed))
                self.assertIsNotNone(hw_resumed._decode_opus_packet(b"\x56\x78"))
                self.assertEqual(hw_resumed._connection_opus_decoder.calls, 1)

        asyncio.run(_scenario())

    def test_05_pc_wake_word_and_exit_command_matching(self):
        """Wakeup & exit matchers handle homophones, fillers, attached commands, and reject mid-sentence false triggers."""
        import tempfile
        import asr_corrections

        class _FakeConn:
            def __init__(self, wakeup_words, cmd_exit=None, selected_asr="SherpaASR", asr_type="sherpa_onnx_local"):
                self.audio_format = "pcm"
                self.config = {
                    "wakeup_words": list(wakeup_words),
                    "selected_module": {"ASR": selected_asr},
                    "ASR": {selected_asr: {"type": asr_type}},
                }
                if cmd_exit is not None:
                    self.cmd_exit = list(cmd_exit)
                self.asr = None

        conn = _FakeConn(["你好芽衣", "你好牙医", "你好小智", "你好"])

        with tempfile.TemporaryDirectory() as td:
            corr_path = os.path.join(td, "asr_corrections.json")
            asr_corrections.save_asr_corrections({"你好小志": "你好小智", "腿出": "退出"}, enabled=True, path=corr_path)
            with patch.object(asr_corrections, "ASR_CORRECTIONS_PATH", corr_path):
                # Pure wakeups (longest-first, punctuation, SenseVoice tags, trailing particles, homophones)
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "你好芽衣"), ("你好芽衣", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "你好，芽衣！"), ("你好芽衣", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "<|zh|><|NEUTRAL|>你好芽衣呀"), ("你好芽衣", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "你好牙医"), ("你好牙医", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "你好小志"), ("你好小智", ""))

                # Leading 1-2 char interjection/filler allowed
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "嗯，你好芽衣"), ("你好芽衣", ""))
                self.assertEqual(
                    pc_pcm_input.match_pc_wakeup_word(conn, "喂，你好芽衣，明天杭州天气怎么样？"),
                    ("你好芽衣", "明天杭州天气怎么样？"),
                )
                self.assertEqual(
                    pc_pcm_input.match_pc_wakeup_word(conn, "那个你好芽衣帮我查一下时间"),
                    ("你好芽衣", "帮我查一下时间"),
                )

                # Attached command at start
                self.assertEqual(
                    pc_pcm_input.match_pc_wakeup_word(conn, "你好芽衣，明天杭州天气怎么样"),
                    ("你好芽衣", "明天杭州天气怎么样"),
                )

                # Anti-false-positive: mid-sentence mentions or non-filler prefixes MUST NOT trigger
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "我刚才提到了你好芽衣这个唤醒词"), ("", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "不要叫你好芽衣"), ("", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "你说你好芽衣试试"), ("", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, ""), ("", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(conn, "   。。。  "), ("", ""))

                # Exact exit commands (including homophone correction)
                for exact_cmd in ("退出", "退出。", "关闭", "退出对话！", "退下", "休眠", "进入待命", "回待命", "腿出"):
                    self.assertTrue(
                        bool(pc_pcm_input.match_pc_exit_command(conn, exact_cmd)),
                        f"Expected exit match for {exact_cmd!r}",
                    )

                # Anti-false-positive: partial/sentence exit mentions MUST NOT exit
                for non_exit in ("我想退出登录", "帮我关闭浏览器", "电脑怎么休眠", "怎么退出这个游戏", ""):
                    self.assertEqual(
                        pc_pcm_input.match_pc_exit_command(conn, non_exit),
                        "",
                        f"Unexpected exit match for {non_exit!r}",
                    )

        # Local vs cloud ASR detection
        self.assertTrue(pc_pcm_input.is_local_asr_provider(conn))
        cloud_conn = _FakeConn(["你好芽衣"], selected_asr="SiliconFlowASR", asr_type="openai")
        self.assertFalse(pc_pcm_input.is_local_asr_provider(cloud_conn))

    def test_06_pc_wake_standby_lifecycle_wakeup_exit_and_memory_flush(self):
        """End-to-end PC wake-standby: hardware preemption, silent noise drop, never-timeout, wakeup, zero-double-decode, exit flush & second wake."""
        import core.handle.helloHandle as hello_mod
        import core.handle.receiveAudioHandle as recv_mod
        import core.handle.sendAudioHandle as send_mod
        import core.handle.textHandler.listenMessageHandler as listen_mod
        from core.connection import ConnectionHandler
        from core.providers.asr.base import ASRProviderBase
        from core.utils.dialogue import Dialogue, Message
        import mei_emotion_router
        import pc_audio_router
        import vad_speculative_router
        from patches import memory_hooks

        pc_audio_router.apply_pc_audio_playback_patches()
        mei_emotion_router.apply_mei_wakeup_responses_patch()
        mei_emotion_router.apply_idle_farewell_patch()
        vad_speculative_router.apply_vad_speculative_patch()
        pc_pcm_input.install_pc_pcm_input_patch()

        class _FakeWs:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.closed = False

            async def send(self, msg: str) -> None:
                self.sent.append(msg)

            async def close(self) -> None:
                self.closed = True

            def json_messages(self) -> list[dict]:
                out = []
                for raw in self.sent:
                    try:
                        out.append(json.loads(raw))
                    except Exception:
                        pass
                return out

        class _NoopLogger:
            def bind(self, **_kwargs):
                return self

            def debug(self, *_args, **_kwargs):
                return None

            def info(self, *_args, **_kwargs):
                return None

            def warning(self, *_args, **_kwargs):
                return None

            def error(self, *_args, **_kwargs):
                return None

        class _FakeSherpaASR(ASRProviderBase):
            def __init__(self) -> None:
                super().__init__()
                self.delete_audio_file = True
                self.output_dir = "/tmp"
                self.next_text = ""
                self.decode_calls = 0
                self.stt_calls = 0

            def _speculative_decode_pcm(self, pcm_bytes: bytes) -> str:
                self.decode_calls += 1
                return self.next_text

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                self.stt_calls += 1
                return self.next_text, None

        async def _scenario() -> None:
            loop = asyncio.get_running_loop()
            listen_handler = listen_mod.ListenTextMessageHandler()

            hw_conn = object.__new__(ConnectionHandler)
            hw_conn.device_id = "aa:bb:cc:dd:ee:01"
            hw_conn.audio_format = "opus"
            hw_conn.loop = loop
            hw_conn.logger = _NoopLogger()
            hw_conn.websocket = _FakeWs()
            hw_conn.stop_event = threading.Event()
            hw_conn.client_abort = False
            hw_conn.client_is_speaking = False
            hw_conn.audio_buffer = []
            hw_conn.reset_audio_states = lambda: None
            hw_conn.clear_queues = lambda: None

            pc_conn = object.__new__(ConnectionHandler)
            pc_conn.device_id = "pc-talk-local"
            pc_conn.session_id = "sid-pc-wake-1"
            pc_conn.audio_format = "pcm"
            pc_conn.loop = loop
            pc_conn.logger = _NoopLogger()
            pc_conn.websocket = _FakeWs()
            pc_conn.stop_event = threading.Event()
            pc_conn.client_abort = False
            pc_conn.client_is_speaking = False
            pc_conn.client_have_voice = False
            pc_conn.client_voice_stop = False
            pc_conn.last_is_voice = False
            pc_conn.client_listen_mode = "auto"
            pc_conn.just_woken_up = False
            pc_conn.close_after_chat = False
            pc_conn.last_activity_time = time.time() * 1000.0
            pc_conn.timeout_seconds = 180
            pc_conn.sentence_id = "turn-0"
            pc_conn.voiceprint_provider = None
            pc_conn.read_config_from_api = False
            pc_conn.asr_audio = []
            pc_conn.audio_buffer = []
            pc_conn.client_audio_buffer = bytearray()
            pc_conn.client_voice_window = []
            pc_conn.cmd_exit = ["退出", "关闭"]
            pc_conn.config = {
                "wakeup_words": ["你好芽衣", "你好小智"],
                "enable_wakeup_words_response_cache": True,
                "enable_greeting": True,
                "selected_module": {"ASR": "SherpaASR"},
                "ASR": {"SherpaASR": {"type": "sherpa_onnx_local"}},
            }
            pc_conn.dialogue = Dialogue()
            pc_conn.dialogue.put(Message(role="system", content="你是雷电芽衣"))
            pc_conn.dialogue.put(Message(role="user", content="fewshot-u", is_temporary=True))
            pc_conn.dialogue.put(Message(role="assistant", content="fewshot-a", is_temporary=True))
            pc_conn.asr = _FakeSherpaASR()
            pc_conn.tts = object()
            pc_conn._closed_called = 0

            async def _track_close(ws=None):
                pc_conn._closed_called += 1
                await pc_conn.websocket.close()

            pc_conn.close = _track_close
            pc_conn.clearSpeakStatus = lambda: setattr(pc_conn, "client_is_speaking", False)

            runtime_events.register_connection(hw_conn)
            runtime_events.register_connection(pc_conn)

            start_to_chat_calls: list[str] = []
            wakeup_check_calls: list[str] = []
            flushed_pairs: list[tuple] = []

            async def _fake_start_to_chat(c, text):
                start_to_chat_calls.append(text)
                c.dialogue.put(Message(role="user", content=text))
                c.dialogue.put(Message(role="assistant", content=f"回复:{text}"))

            async def _fake_check_wakeup(c, text):
                wakeup_check_calls.append(text)
                c.just_woken_up = True
                c.sentence_id = "wake-greeting-sid"
                c.dialogue.put(Message(role="assistant", content="我在呢，哥哥请说吧。"))
                return True

            def _fake_flush_pair(dlg, *, messages_slice=None, mark_closed=False):
                flushed_pairs.append((len(dlg.dialogue), mark_closed))
                return True

            pcm_clip = [b"\x01\x02" * 960 * 16]

            with (
                patch.object(recv_mod, "startToChat", side_effect=_fake_start_to_chat),
                patch("core.providers.asr.base.startToChat", side_effect=_fake_start_to_chat),
                patch.object(hello_mod, "checkWakeupWords", side_effect=_fake_check_wakeup),
                patch.object(memory_hooks, "_flush_dialogue_latest_pair", side_effect=_fake_flush_pair),
                patch("patches.emotion.wakeup.sync_wakeup_words_from_yaml", return_value=False),
            ):
                # 1. Enter wake-standby mode -> preempts hardware ESP32 & pushes pc_mic_state("standby")
                await listen_handler.handle(
                    pc_conn,
                    {"type": "listen", "state": "start", "mode": "auto", "pc_wake_mode": True},
                )
                await asyncio.sleep(0.05)

                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))
                self.assertTrue(pc_pcm_input.is_pc_mic_connection_active(pc_conn))
                self.assertTrue(hw_conn.websocket.closed)
                self.assertGreaterEqual(pc_conn.timeout_seconds, 3600)
                st_msgs = [m for m in pc_conn.websocket.json_messages() if m.get("type") == "pc_mic_state"]
                self.assertEqual(st_msgs[-1]["state"], "standby")
                self.assertTrue(st_msgs[-1]["local_asr"])
                self.assertIn("你好芽衣", st_msgs[-1]["wakeup_words"])
                runtime_events.unregister_connection(hw_conn)

                # 2. Casual chatter in standby -> speculative decode does NOT early-commit, handle_voice_stop silently drops it
                pc_conn.asr.next_text = "今天中午吃什么呢"
                spec_st = vad_speculative_router._get_spec_state(pc_conn)
                await vad_speculative_router._run_speculative_decode_task(
                    pc_conn, pc_conn.asr, spec_st, spec_st.generation, pcm_clip[0], {"enabled": True}, False
                )
                self.assertFalse(spec_st.should_commit)
                self.assertEqual(spec_st.reason, "pc_wake_standby_non_wake")
                # Simulate 1100ms cutoff handoff reusing speculative decode (ZERO second Sherpa decode!)
                spec_st.committed_text = spec_st.result_text
                spec_st.committed_pcm_len = spec_st.result_pcm_len
                calls_before = pc_conn.asr.decode_calls
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertEqual(pc_conn.asr.decode_calls, calls_before)
                self.assertEqual(pc_conn.asr.stt_calls, 0)
                self.assertEqual(start_to_chat_calls, [])
                self.assertEqual(wakeup_check_calls, [])
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))

                # 3. Standby never times out even when last_activity_time is 600s old
                pc_conn.last_activity_time = (time.time() - 600.0) * 1000.0
                await recv_mod.no_voice_close_connect(pc_conn, False)
                self.assertEqual(pc_conn._closed_called, 0)
                self.assertGreater(pc_conn.last_activity_time, (time.time() - 5.0) * 1000.0)
                self.assertGreaterEqual(pc_conn.timeout_seconds, 3600)

                # 4. Pure wakeup word "你好芽衣" -> speculative early-commit + wakes & plays greeting
                pc_conn.asr.next_text = "你好芽衣"
                spec_st.invalidate(count_discard=False)
                await vad_speculative_router._run_speculative_decode_task(
                    pc_conn, pc_conn.asr, spec_st, spec_st.generation, pcm_clip[0], {"enabled": True}, False
                )
                self.assertTrue(spec_st.should_commit)
                self.assertTrue(spec_st.reason.startswith("pc_wake_word:"))
                spec_st.committed_text = spec_st.result_text
                spec_st.committed_pcm_len = spec_st.result_pcm_len
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)

                self.assertTrue(pc_pcm_input.is_pc_wake_awake(pc_conn))
                self.assertEqual(wakeup_check_calls, ["你好芽衣"])
                self.assertTrue(pc_conn.just_woken_up)
                self.assertTrue(pc_conn._vad_post_wakeup_pending)
                self.assertLess(pc_conn.timeout_seconds, 3600)

                # Finishing wakeup greeting TTS stop immediately clears just_woken_up on PCM conn
                await send_mod.send_tts_message(pc_conn, "stop", None)
                self.assertFalse(pc_conn.just_woken_up)

                # 5. Normal awake utterance -> zero double-decode & passes straight into startToChat
                pc_conn.asr.next_text = "明天杭州天气怎么样"
                spec_st.invalidate(count_discard=False)
                calls_before = pc_conn.asr.decode_calls
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertEqual(pc_conn.asr.decode_calls, calls_before + 1)
                self.assertEqual(pc_conn.asr.stt_calls, 0)
                self.assertEqual(start_to_chat_calls, ["明天杭州天气怎么样"])

                # 6. Say "退出" while awake -> flushes Anima memory, trims dialogue keeping system+fewshot, returns to standby WITHOUT closing WS
                pc_conn.asr.next_text = "退出。"
                spec_st.invalidate(count_discard=False)
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertEqual(pc_conn._closed_called, 0)
                self.assertFalse(pc_conn.websocket.closed)
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))
                self.assertEqual(len(flushed_pairs), 1)
                self.assertTrue(flushed_pairs[0][1])  # mark_closed=True
                roles_left = [(m.role, m.is_temporary) for m in pc_conn.dialogue.dialogue]
                self.assertEqual(roles_left, [("system", False), ("user", True), ("assistant", True)])
                st_msgs = [m for m in pc_conn.websocket.json_messages() if m.get("type") == "pc_mic_state"]
                self.assertEqual(st_msgs[-1]["state"], "standby")
                self.assertEqual(st_msgs[-1]["reason"], "voice_exit")

                # 7. Second wake with attached command -> wakes and forwards remainder directly to startToChat
                pc_conn.asr.next_text = "你好芽衣，帮我查一下明天天气"
                spec_st.invalidate(count_discard=False)
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertTrue(pc_pcm_input.is_pc_wake_awake(pc_conn))
                self.assertEqual(start_to_chat_calls[-1], "帮我查一下明天天气")
                self.assertEqual(len(wakeup_check_calls), 1)  # no extra pure greeting

                # 8. Switch off PC mic (`pc_mic_state="off"`) -> exits wake mode & releases hardware exclusivity
                await listen_handler.handle(
                    pc_conn,
                    {"type": "listen", "state": "stop", "pc_mic_state": "off"},
                )
                self.assertFalse(pc_pcm_input.is_pc_wake_mode(pc_conn))
                self.assertFalse(pc_pcm_input.has_active_pc_mic_connection())

        asyncio.run(_scenario())

    def test_07_pc_wake_close_after_chat_and_idle_timeout_return_to_standby(self):
        """Farewell / handle_exit_intent (`close_after_chat=True`) and silent idle timeout return PC wake mode to standby without closing WS."""
        import core.handle.receiveAudioHandle as recv_mod
        import core.handle.sendAudioHandle as send_mod
        import core.handle.textHandler.listenMessageHandler as listen_mod
        from core.connection import ConnectionHandler
        from core.utils.dialogue import Dialogue, Message
        import mei_emotion_router
        import pc_audio_router
        import runtime_settings
        from patches import memory_hooks

        pc_audio_router.apply_pc_audio_playback_patches()
        mei_emotion_router.apply_mei_wakeup_responses_patch()
        mei_emotion_router.apply_idle_farewell_patch()
        pc_pcm_input.install_pc_pcm_input_patch()

        class _FakeWs:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.closed = False

            async def send(self, msg: str) -> None:
                self.sent.append(msg)

            async def close(self) -> None:
                self.closed = True

        class _NoopLogger:
            def bind(self, **_kwargs):
                return self

            def debug(self, *_args, **_kwargs):
                return None

            def info(self, *_args, **_kwargs):
                return None

            def warning(self, *_args, **_kwargs):
                return None

            def error(self, *_args, **_kwargs):
                return None

        async def _scenario() -> None:
            loop = asyncio.get_running_loop()
            listen_handler = listen_mod.ListenTextMessageHandler()

            pc_conn = object.__new__(ConnectionHandler)
            pc_conn.device_id = "pc-talk-local"
            pc_conn.session_id = "sid-pc-wake-2"
            pc_conn.audio_format = "pcm"
            pc_conn.loop = loop
            pc_conn.logger = _NoopLogger()
            pc_conn.websocket = _FakeWs()
            pc_conn.stop_event = threading.Event()
            pc_conn.client_abort = False
            pc_conn.client_is_speaking = False
            pc_conn.close_after_chat = False
            pc_conn.just_woken_up = False
            pc_conn.last_activity_time = time.time() * 1000.0
            pc_conn.timeout_seconds = 180
            pc_conn.sentence_id = "farewell-sid-1"
            pc_conn.config = {"wakeup_words": ["你好芽衣"]}
            pc_conn.dialogue = Dialogue()
            pc_conn.dialogue.put(Message(role="system", content="persona"))
            pc_conn.dialogue.put(Message(role="user", content="拜拜"))
            pc_conn.dialogue.put(Message(role="assistant", content="晚安哥哥"))
            pc_conn.tts = None
            pc_conn.audio_rate_controller = None
            pc_conn._closed_called = 0

            async def _track_close(ws=None):
                pc_conn._closed_called += 1

            pc_conn.close = _track_close
            pc_conn.clearSpeakStatus = lambda: setattr(pc_conn, "client_is_speaking", False)
            pc_conn.reset_audio_states = lambda: None
            pc_conn.clear_queues = lambda: None

            runtime_events.register_connection(pc_conn)

            with (
                patch.object(memory_hooks, "_flush_dialogue_latest_pair", return_value=True),
                patch("patches.emotion.wakeup.sync_wakeup_words_from_yaml", return_value=False),
            ):
                # 1. Awake + handle_exit_intent sets close_after_chat=True -> send_tts_message("stop") intercepts & returns to standby
                await pc_pcm_input.enter_pc_wake_awake(pc_conn, wake_word="你好芽衣")
                pc_conn.close_after_chat = True
                await send_mod.send_tts_message(pc_conn, "stop", None)

                self.assertFalse(pc_conn.close_after_chat)
                self.assertEqual(pc_conn._closed_called, 0)
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))
                self.assertEqual([m.role for m in pc_conn.dialogue.dialogue], ["system"])

                # 2. Same interception when PC speaker stop branch (`is_pc_stop=True`) runs
                await pc_pcm_input.enter_pc_wake_awake(pc_conn, wake_word="你好芽衣")
                pc_conn.sentence_id = "farewell-sid-pc"
                pc_audio_router._ensure_conn_pc_state(pc_conn)
                pc_conn._pc_audio_active_sid = "farewell-sid-pc"
                pc_conn._pc_audio_queued_ms = 500
                pc_conn.close_after_chat = True
                with patch.object(pc_audio_router, "_wait_pc_audio_done", return_value=True):
                    await send_mod.send_tts_message(pc_conn, "stop", None)
                self.assertFalse(pc_conn.close_after_chat)
                self.assertEqual(pc_conn._closed_called, 0)
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))

                # 3. Awake idle timeout with farewell disabled -> standby instead of conn.close()
                await pc_pcm_input.enter_pc_wake_awake(pc_conn, wake_word="你好芽衣")
                pc_conn.last_activity_time = (time.time() - 300.0) * 1000.0
                with patch.object(
                    runtime_settings,
                    "get_idle_farewell",
                    return_value={"enabled": False, "timeout_seconds": 60, "mode": "fixed", "messages": []},
                ):
                    await recv_mod.no_voice_close_connect(pc_conn, False)
                self.assertEqual(pc_conn._closed_called, 0)
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))

                # 4. Awake idle timeout with farewell enabled but TTS missing -> standby instead of conn.close()
                await pc_pcm_input.enter_pc_wake_awake(pc_conn, wake_word="你好芽衣")
                pc_conn.last_activity_time = (time.time() - 300.0) * 1000.0
                with patch.object(
                    runtime_settings,
                    "get_idle_farewell",
                    return_value={"enabled": True, "timeout_seconds": 60, "mode": "fixed", "messages": ["拜拜啦"]},
                ):
                    await recv_mod.no_voice_close_connect(pc_conn, False)
                self.assertEqual(pc_conn._closed_called, 0)
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))

                # 5. pc_wake_action="keep_awake" keeps awake; pc_wake_action="sleep" returns to standby
                await pc_pcm_input.enter_pc_wake_awake(pc_conn, wake_word="你好芽衣")
                await listen_handler.handle(
                    pc_conn,
                    {
                        "type": "listen",
                        "state": "start",
                        "mode": "auto",
                        "pc_wake_mode": True,
                        "pc_wake_action": "keep_awake",
                    },
                )
                self.assertTrue(pc_pcm_input.is_pc_wake_awake(pc_conn))

                await listen_handler.handle(
                    pc_conn,
                    {"type": "listen", "state": "start", "mode": "auto", "pc_wake_action": "sleep"},
                )
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))

                # 6. Active speaking aborted cleanly when returning to standby (manual_sleep / voice_exit):
                #    keeps client_abort=True and rotates sentence_id so any concurrent LLM/TTS thread stops cleanly,
                #    then clears client_abort=False upon next wakeup.
                await pc_pcm_input.enter_pc_wake_awake(pc_conn, wake_word="你好芽衣")
                pc_conn.sentence_id = "active-speaking-sid"
                pc_conn.client_is_speaking = True
                pc_conn.close_after_chat = True
                with patch.object(pc_audio_router, "_post_stop_to_pc", return_value=None):
                    await pc_pcm_input.enter_pc_wake_standby(
                        pc_conn,
                        reason="voice_exit",
                        trigger_text="退出",
                        flush_session=True,
                        abort_speaking=True,
                    )
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))
                self.assertFalse(pc_conn.client_is_speaking)
                self.assertTrue(pc_conn.client_abort)
                self.assertNotEqual(pc_conn.sentence_id, "active-speaking-sid")
                self.assertFalse(pc_conn.close_after_chat)
                await pc_pcm_input.enter_pc_wake_awake(pc_conn, wake_word="你好芽衣")
                self.assertFalse(pc_conn.client_abort)

        asyncio.run(_scenario())

    def test_08_pc_wake_non_sherpa_asr_single_decode_and_exit(self):
        """Non-Sherpa (e.g. cloud HTTP / FunASR dict) ASR works in PC wake standby & awake without double API calls."""
        import core.handle.receiveAudioHandle as recv_mod
        from core.connection import ConnectionHandler
        from core.providers.asr.base import ASRProviderBase
        from core.utils.dialogue import Dialogue, Message
        import vad_speculative_router
        from patches import memory_hooks

        vad_speculative_router.apply_vad_speculative_patch()
        pc_pcm_input.install_pc_pcm_input_patch()

        class _FakeWs:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.closed = False

            async def send(self, msg: str) -> None:
                self.sent.append(msg)

        class _NoopLogger:
            def bind(self, **_kwargs):
                return self

            def debug(self, *_args, **_kwargs):
                return None

            def info(self, *_args, **_kwargs):
                return None

            def warning(self, *_args, **_kwargs):
                return None

            def error(self, *_args, **_kwargs):
                return None

        class _FakeCloudASR(ASRProviderBase):
            __module__ = "core.providers.asr.openai"

            def __init__(self) -> None:
                super().__init__()
                self.delete_audio_file = True
                self.output_dir = "/tmp"
                self.next_result = ""
                self.stt_calls = 0

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                self.stt_calls += 1
                return self.next_result, None

        async def _scenario() -> None:
            loop = asyncio.get_running_loop()
            pc_conn = object.__new__(ConnectionHandler)
            pc_conn.device_id = "pc-talk-local"
            pc_conn.session_id = "sid-pc-cloud-wake"
            pc_conn.audio_format = "pcm"
            pc_conn.loop = loop
            pc_conn.logger = _NoopLogger()
            pc_conn.websocket = _FakeWs()
            pc_conn.stop_event = threading.Event()
            pc_conn.client_abort = False
            pc_conn.client_is_speaking = False
            pc_conn.close_after_chat = False
            pc_conn.just_woken_up = False
            pc_conn.last_activity_time = time.time() * 1000.0
            pc_conn.timeout_seconds = 180
            pc_conn.voiceprint_provider = None
            pc_conn.read_config_from_api = False
            pc_conn.cmd_exit = ["退出", "关闭"]
            pc_conn.config = {"wakeup_words": ["你好芽衣"], "enable_greeting": True}
            pc_conn.dialogue = Dialogue()
            pc_conn.dialogue.put(Message(role="system", content="persona"))
            pc_conn.asr = _FakeCloudASR()
            pc_conn.tts = object()
            pc_conn.reset_audio_states = lambda: None
            pc_conn.clear_queues = lambda: None

            chat_calls: list[str] = []

            async def _fake_chat(c, text):
                chat_calls.append(text)

            pcm_clip = [b"\x01\x02" * 960 * 16]

            with (
                patch.object(recv_mod, "startToChat", side_effect=_fake_chat),
                patch("core.providers.asr.base.startToChat", side_effect=_fake_chat),
                patch.object(memory_hooks, "_flush_dialogue_latest_pair", return_value=True),
                patch("patches.emotion.wakeup.sync_wakeup_words_from_yaml", return_value=False),
            ):
                await pc_pcm_input.enter_pc_wake_standby(pc_conn, reason="client_start")
                self.assertFalse(pc_pcm_input.is_local_asr_provider(pc_conn))

                # 1. Standby non-wake speech -> single cloud ASR call, silently dropped
                pc_conn.asr.next_result = "随便说一句"
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertEqual(pc_conn.asr.stt_calls, 1)
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))
                self.assertEqual(chat_calls, [])

                # 2. Standby wakeup with attached command (dict format like FunASR) -> wakes & routes command
                pc_conn.asr.next_result = {"content": "你好芽衣，帮我看下时间", "language": "zh"}
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertEqual(pc_conn.asr.stt_calls, 2)
                self.assertTrue(pc_pcm_input.is_pc_wake_awake(pc_conn))
                self.assertEqual(chat_calls, ["帮我看下时间"])

                # 3. Awake normal utterance -> EXACTLY 1 speech_to_text call (override cache prevents double cloud call!)
                pc_conn.asr.next_result = "今天星期几"
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertEqual(pc_conn.asr.stt_calls, 3)
                self.assertEqual(chat_calls[-1], "今天星期几")

                # 4. Awake exit command -> returns to standby without closing WS
                pc_conn.asr.next_result = "休眠"
                await pc_conn.asr.handle_voice_stop(pc_conn, pcm_clip)
                self.assertEqual(pc_conn.asr.stt_calls, 4)
                self.assertTrue(pc_pcm_input.is_pc_wake_standby(pc_conn))
                self.assertFalse(pc_conn.websocket.closed)

        asyncio.run(_scenario())

    def test_09_console_pc_wake_toggle_switch_ui_wiring(self):
        """18080 console exposes a toggleable switch (`pcMicWakeToggle` role='switch'), preserves all legacy PC mic IDs, and passes Node.js VM state transitions."""
        import shutil
        import subprocess

        static_dir = os.path.join(APP_DIR, "console", "static")
        html_path = os.path.join(static_dir, "index.html")
        js_path = os.path.join(static_dir, "app.js")
        css_path = os.path.join(static_dir, "app.css")
        with open(html_path, "r", encoding="utf-8") as fh:
            html_src = fh.read()
        with open(js_path, "r", encoding="utf-8") as fj:
            js_src = fj.read()
        with open(css_path, "r", encoding="utf-8") as fc:
            css_src = fc.read()

        for elem_id in (
            "pcMicWakeRow",
            "pcMicWakeToggle",
            "pcMicWakeBadge",
            "pcMicWakeHint",
            "pcMicToggleBtn",
            "pcMicToggleLabel",
            "pcMicPttBtn",
            "pcMicInterruptBtn",
            "pcMicFullDuplexToggle",
            "pcMicStatusText",
            "pcMicLevelFill",
        ):
            self.assertIn(f'id="{elem_id}"', html_src, f"Missing DOM ID {elem_id} in index.html")
            self.assertIn(elem_id, js_src, f"DOM ID {elem_id} not wired in app.js")

        self.assertIn('id="pcMicWakeToggle" role="switch"', html_src)
        self.assertIn("pc_wake_mode", js_src)
        self.assertIn("pc_mic_state", js_src)
        self.assertIn(".pc-mic-wake-row.is-standby", css_src)
        self.assertIn(".pc-mic-wake-row.is-awake", css_src)

        node_bin = shutil.which("node")
        if os.environ.get("XIAOZHI_REQUIRE_NODE_VM") == "1":
            self.assertIsNotNone(node_bin, "XIAOZHI_REQUIRE_NODE_VM=1 set but node binary not found in PATH")
        if node_bin:
            harness = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const jsPath = process.argv[1];
let src = fs.readFileSync(jsPath, 'utf8');
src = src.replace(/\}\)\(\);\s*$/, 'globalThis.__H__ = { els, pcMic, stopPcMic }; })();');
function makeEl(id) {
  const ls = {};
  const attrs = {};
  return {
    id, textContent: '', innerHTML: '', value: '', checked: false, disabled: false, className: '', title: '', style: {}, dataset: {}, children: [],
    classList: {
      _s: new Set(),
      add(...x){x.forEach(a=>this._s.add(a));},
      remove(...x){x.forEach(a=>this._s.delete(a));},
      toggle(a,f){if(f===undefined){this._s.has(a)?this._s.delete(a):this._s.add(a);}else if(f)this._s.add(a);else this._s.delete(a);},
      contains(a){return this._s.has(a);}
    },
    appendChild(c){this.children.push(c);return c;},
    addEventListener(ev, fn){(ls[ev]=ls[ev]||[]).push(fn);},
    async trigger(ev, e={}){for(const fn of (ls[ev]||[])) await fn(e);},
    querySelectorAll(){return [];}, querySelector(){return null;},
    getAttribute(k){return attrs[k] || '';}, setAttribute(k, v){attrs[k] = String(v);},
    focus(){}, pause(){}, play(){return Promise.resolve();}
  };
}
const elMap = new Map();
const wsInstances = [];
class FakeWS {
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; this.closed = false; wsInstances.push(this); }
  send(d) { this.sent.push(d); }
  close() { this.closed = true; this.readyState = 3; }
}
class FakeAudioCtx {
  constructor() { this.sampleRate = 48000; this.state = 'running'; this.destination = {}; }
  createMediaStreamSource() { return { connect(){}, disconnect(){} }; }
  createGain() { return { gain: { value: 0 }, connect(){}, disconnect(){} }; }
  createScriptProcessor() { return { connect(){}, disconnect(){}, onaudioprocess: null }; }
  close() {}
}
const timers = [];
let allowMic = true;
let pendingGumResolve = null;
let stoppedTracks = 0;
const sandbox = {
  console, URLSearchParams, encodeURIComponent, Int16Array,
  setTimeout(fn, ms) { const id = timers.length + 1; timers.push({ id, fn, ms, cleared: false }); return id; },
  clearTimeout(id) { const t = timers.find(x => x.id === id); if (t) t.cleared = true; },
  setInterval: () => 1, clearInterval: () => {},
  WebSocket: FakeWS,
  navigator: {
    clipboard: { writeText: async () => {} },
    mediaDevices: {
      getUserMedia: () => {
        if (!allowMic) return Promise.reject(new Error('NotAllowedError'));
        const mkStream = () => ({ getTracks: () => [{ stop(){ stoppedTracks++; } }] });
        if (pendingGumResolve === 'arm') {
          return new Promise((resolve) => { pendingGumResolve = () => resolve(mkStream()); });
        }
        return Promise.resolve(mkStream());
      }
    }
  },
  window: { addEventListener(){}, location: { origin: 'http://127.0.0.1:18080', protocol: 'http:', host: '127.0.0.1:18080' }, AudioContext: FakeAudioCtx },
  document: {
    hidden: true, visibilityState: 'hidden',
    getElementById(id) { if (!elMap.has(id)) elMap.set(id, makeEl(id)); return elMap.get(id); },
    querySelector(sel) { return sel === 'meta[name="console-token"]' ? { getAttribute: () => 'tok' } : null; },
    querySelectorAll() { return []; }, createElement() { return makeEl(''); }, addEventListener() {},
  },
  fetch: async (url) => {
    if (url === '/api/pc-mic/session') return { ok: true, status: 200, json: async () => ({ ok: true, ws_path: '/ws/pc-mic?ticket=t1', pc_audio_online: true }) };
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });
(async () => {
  const { els, pcMic, stopPcMic } = sandbox.__H__;
  // 0. Permission denied -> switch snaps back OFF cleanly
  allowMic = false;
  els.pcMicWakeToggle.checked = true;
  await els.pcMicWakeToggle.trigger('change');
  await new Promise(r => setImmediate(r));
  assert.strictEqual(els.pcMicWakeToggle.checked, false);
  assert.strictEqual(els.pcMicWakeBadge.textContent, '已关闭');
  allowMic = true;

  // 1. Toggle ON -> starts wake session & enters standby
  els.pcMicWakeToggle.checked = true;
  await els.pcMicWakeToggle.trigger('change');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  assert.strictEqual(wsInstances.length, 1);
  const ws = wsInstances[0];
  ws.onopen();
  assert.strictEqual(JSON.parse(ws.sent[0]).audio_params.format, 'pcm');
  ws.onmessage({ data: JSON.stringify({ type: 'hello', session_id: 's1' }) });
  assert.strictEqual(JSON.parse(ws.sent[1]).pc_wake_mode, true);

  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'standby', reason: 'client_start', wakeup_words: ['你好芽衣'], local_asr: true }) });
  assert.strictEqual(els.pcMicWakeToggle.checked, true);
  assert.strictEqual(els.pcMicWakeToggle.getAttribute('aria-checked'), 'true');
  assert.strictEqual(els.pcMicWakeRow.classList.contains('is-standby'), true);
  assert.strictEqual(els.pcMicWakeBadge.textContent, '待命监听');
  assert.strictEqual(els.pcMicInterruptBtn.disabled, true);
  assert.strictEqual(els.pcMicToggleLabel.textContent, '开麦自由聊');
  assert.ok(els.pcMicStatusText.textContent.includes('你好芽衣'));
  assert.strictEqual(els.pcMicStatusText.title, els.pcMicStatusText.textContent);

  // 2. Wake -> awake state + interrupt keeps awake
  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'awake', reason: 'wake_word', wake_word: '你好芽衣', wakeup_words: ['你好芽衣'], local_asr: true }) });
  assert.strictEqual(els.pcMicWakeRow.classList.contains('is-awake'), true);
  assert.strictEqual(els.pcMicWakeBadge.textContent, '已唤醒对话');
  assert.strictEqual(els.pcMicInterruptBtn.disabled, false);

  await els.pcMicInterruptBtn.trigger('click');
  const intrMsgs = ws.sent.slice(-2).map(x => JSON.parse(x));
  assert.strictEqual(intrMsgs[0].type, 'abort');
  assert.strictEqual(intrMsgs[1].pc_wake_action, 'keep_awake');

  // 3. Farewell tts stop + pc_mic_state standby cancels unmute timer & immediately unmutes
  ws.onmessage({ data: JSON.stringify({ type: 'tts', state: 'start' }) });
  assert.strictEqual(pcMic.assistantSpeaking, true);
  ws.onmessage({ data: JSON.stringify({ type: 'tts', state: 'stop' }) });
  const lastTimer = timers[timers.length - 1];
  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'standby', reason: 'chat_exit', wakeup_words: ['你好芽衣'], local_asr: true }) });
  assert.strictEqual(lastTimer.cleared, true);
  assert.strictEqual(pcMic.assistantSpeaking, false);
  assert.strictEqual(els.pcMicWakeRow.classList.contains('is-standby'), true);

  // 4. Cloud ASR notice (status text + warning banner)
  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'standby', reason: 'client_start', wakeup_words: ['你好芽衣'], local_asr: false }) });
  assert.ok(els.pcMicStatusText.textContent.includes('云端 ASR'));
  assert.ok(els.bannerText.textContent.includes('云端语音识别'));

  // 5. Switch from wake(awake, speaking) to free-chat sends abort first (prevents echo) & ignores stale standby
  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'awake', reason: 'wake_word', wake_word: '你好芽衣', local_asr: true }) });
  ws.onmessage({ data: JSON.stringify({ type: 'tts', state: 'start' }) });
  const beforeSwitchLen = ws.sent.length;
  await els.pcMicToggleBtn.trigger('click');
  const swMsgs = ws.sent.slice(beforeSwitchLen).map(x => JSON.parse(x));
  assert.strictEqual(swMsgs[0].type, 'abort');
  assert.strictEqual(swMsgs[1].pc_wake_mode, false);
  assert.strictEqual(els.pcMicWakeToggle.checked, false);
  assert.strictEqual(pcMic.mode, 'auto');
  // Stale in-flight standby event MUST NOT hijack auto mode back to wake
  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'standby', reason: 'chat_exit', wakeup_words: ['你好芽衣'], local_asr: true }) });
  assert.strictEqual(pcMic.mode, 'auto');
  assert.strictEqual(els.pcMicWakeToggle.checked, false);

  // 6. Switch back to wake standby on same WS; server state='off' cleanly clears switch (no stuck checked state)
  els.pcMicWakeToggle.checked = true;
  await els.pcMicWakeToggle.trigger('change');
  assert.strictEqual(pcMic.mode, 'wake');
  assert.strictEqual(JSON.parse(ws.sent[ws.sent.length - 1]).pc_wake_action, 'sleep');

  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'off', reason: 'switch_mode' }) });
  assert.strictEqual(els.pcMicWakeToggle.checked, false);
  assert.strictEqual(els.pcMicWakeBadge.textContent, '已关闭');
  assert.strictEqual(pcMic.mode, 'auto');

  // 7. Re-enter wake, receive replaced_by_new_pc_mic -> auto-stops tab & releases mic
  els.pcMicWakeToggle.checked = true;
  await els.pcMicWakeToggle.trigger('change');
  ws.onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'off', reason: 'replaced_by_new_pc_mic' }) });
  assert.strictEqual(ws.closed, true);
  assert.strictEqual(els.pcMicWakeToggle.checked, false);
  assert.ok(els.pcMicStatusText.textContent.includes('另一标签页'));

  // 8. Toggle wake ON while ws.onopen && !helloDone MUST NOT leak a second WebSocket
  wsInstances.length = 0;
  await els.pcMicToggleBtn.trigger('click');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  assert.strictEqual(wsInstances.length, 1);
  wsInstances[0].onopen();
  els.pcMicWakeToggle.checked = true;
  await els.pcMicWakeToggle.trigger('change');
  await new Promise(r => setImmediate(r));
  assert.strictEqual(wsInstances.length, 1);
  wsInstances[0].onmessage({ data: JSON.stringify({ type: 'hello', session_id: 's2' }) });
  assert.strictEqual(JSON.parse(wsInstances[0].sent[1]).pc_wake_mode, true);

  // 9. Toggle OFF while assistant is speaking sends abort BEFORE pc_mic_state='off' and closes WS cleanly
  wsInstances[0].onmessage({ data: JSON.stringify({ type: 'pc_mic_state', state: 'awake', wake_word: '你好芽衣', local_asr: true }) });
  wsInstances[0].onmessage({ data: JSON.stringify({ type: 'tts', state: 'start' }) });
  const beforeOffLen = wsInstances[0].sent.length;
  els.pcMicWakeToggle.checked = false;
  await els.pcMicWakeToggle.trigger('change');
  assert.strictEqual(wsInstances[0].closed, true);
  const offMsgs = wsInstances[0].sent.slice(beforeOffLen).map(x => JSON.parse(x));
  assert.strictEqual(offMsgs[0].type, 'abort');
  assert.strictEqual(offMsgs[1].pc_mic_state, 'off');
  assert.strictEqual(els.pcMicWakeToggle.checked, false);
  assert.strictEqual(els.pcMicWakeBadge.textContent, '已关闭');

  // 10. stopPcMic called while getUserMedia is pending cancels session & stops acquired tracks without opening WS
  wsInstances.length = 0;
  stoppedTracks = 0;
  pendingGumResolve = 'arm';
  els.pcMicWakeToggle.checked = true;
  const startPromise = els.pcMicWakeToggle.trigger('change');
  await new Promise(r => setImmediate(r));
  stopPcMic(true);
  pendingGumResolve();
  await startPromise;
  await new Promise(r => setImmediate(r));
  assert.strictEqual(wsInstances.length, 0);
  assert.strictEqual(stoppedTracks, 1);
  console.log('NODE_VM_PC_WAKE_PASS');
})().catch(e => { console.error(e); process.exit(1); });
"""
            proc = subprocess.run(
                [node_bin, "-e", harness, js_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Node.js VM pcMicWakeToggle check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )
            self.assertIn("NODE_VM_PC_WAKE_PASS", proc.stdout)

    def test_10_pc_wake_regression_guards_abort_race_double_asr_multitab_and_timeout(self):
        """Regression tests for concurrent-LLM abort, keep_awake VAD blackout, non-Mei punctuated wakeup, multi-tab preemption, ASCII boundaries, double ASR correction, and standby timeout preservation."""
        import tempfile
        import asr_corrections
        import core.handle.abortHandle as abort_mod
        import core.handle.helloHandle as hello_mod
        import core.handle.textHandler.listenMessageHandler as listen_mod
        from core.connection import ConnectionHandler
        from core.providers.asr.base import ASRProviderBase
        import mei_emotion_router
        import pc_audio_router
        import vad_speculative_router
        from console.routes import config as config_routes
        from patches import asr as asr_patches

        pc_audio_router.apply_pc_audio_playback_patches()
        mei_emotion_router.apply_mei_wakeup_responses_patch()
        vad_speculative_router.apply_vad_speculative_patch()
        pc_pcm_input.install_pc_pcm_input_patch()
        asr_patches.install_memory_optimizations(start_trimmer=False)

        class _FakeWs:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.closed = False

            async def send(self, msg: str) -> None:
                self.sent.append(msg)

            async def close(self) -> None:
                self.closed = True

            def json_messages(self) -> list[dict]:
                out = []
                for raw in self.sent:
                    try:
                        out.append(json.loads(raw))
                    except Exception:
                        pass
                return out

        class _NoopLogger:
            def bind(self, **_kwargs):
                return self

            def debug(self, *_args, **_kwargs):
                return None

            def info(self, *_args, **_kwargs):
                return None

            def warning(self, *_args, **_kwargs):
                return None

            def error(self, *_args, **_kwargs):
                return None

        class _FakeDoubaoASR(ASRProviderBase):
            __module__ = "core.providers.asr.doubao"

            def __init__(self) -> None:
                super().__init__()
                self.delete_audio_file = True
                self.output_dir = "/tmp"
                self.next_text = ""
                self.calls = 0

            async def speech_to_text(self, opus_data, session_id, artifacts=None):
                self.calls += 1
                return self.next_text, None

        def _make_pc_conn(sid: str, loop) -> ConnectionHandler:
            c = object.__new__(ConnectionHandler)
            c.device_id = "pc-talk-local"
            c.session_id = sid
            c.audio_format = "pcm"
            c.loop = loop
            c.logger = _NoopLogger()
            c.websocket = _FakeWs()
            c.stop_event = threading.Event()
            c.client_abort = False
            c.client_is_speaking = False
            c.close_after_chat = False
            c.just_woken_up = False
            c.last_activity_time = time.time() * 1000.0
            c.timeout_seconds = 180
            c.sentence_id = f"turn-{sid}"
            c.client_listen_mode = "auto"
            c.voiceprint_provider = None
            c.read_config_from_api = False
            c.cmd_exit = ["退出", "关闭"]
            c.config = {
                "wakeup_words": ["你好，芽衣", "Hey Mei"],
                "enable_wakeup_words_response_cache": True,
                "enable_greeting": True,
            }
            c.asr_audio = []
            c.audio_buffer = []
            c.client_audio_buffer = bytearray()
            c.client_voice_window = []
            c.tts = None
            c.reset_audio_states = lambda: None
            c.clear_queues = lambda: None
            c.clearSpeakStatus = lambda: setattr(c, "client_is_speaking", False)
            return c

        async def _scenario() -> None:
            loop = asyncio.get_running_loop()
            lh = listen_mod.ListenTextMessageHandler()

            # 1. ASCII wakeup word boundary guard
            ascii_conn = _make_pc_conn("ascii", loop)
            ascii_conn.config = {"wakeup_words": ["hi", "hey"]}
            with patch("patches.emotion.wakeup.sync_wakeup_words_from_yaml", return_value=False):
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(ascii_conn, "high quality"), ("", ""))
                self.assertEqual(pc_pcm_input.match_pc_wakeup_word(ascii_conn, "hi there"), ("hi", "there"))

            # 2. Interrupt during wakeup greeting (abort + keep_awake) clears just_woken_up & cancels vad_resume_task
            c1 = _make_pc_conn("c1", loop)
            runtime_events.register_connection(c1)
            with patch("patches.emotion.wakeup.sync_wakeup_words_from_yaml", return_value=False):
                await pc_pcm_input.enter_pc_wake_awake(c1, wake_word="你好芽衣")
                c1.just_woken_up = True
                c1._vad_post_wakeup_pending = True
                c1.client_is_speaking = True
                c1.vad_resume_task = asyncio.create_task(asyncio.sleep(10))
                with patch.object(pc_audio_router, "_post_stop_to_pc", return_value=None):
                    await abort_mod.handleAbortMessage(c1)
                self.assertFalse(c1.just_woken_up)
                await asyncio.sleep(0)
                self.assertTrue(c1.vad_resume_task.cancelled())

                c1.just_woken_up = True
                c1.last_activity_time = (time.time() - 500.0) * 1000.0
                c1.vad_resume_task = asyncio.create_task(asyncio.sleep(10))
                await lh.handle(
                    c1,
                    {"type": "listen", "state": "start", "mode": "auto", "pc_wake_mode": True, "pc_wake_action": "keep_awake"},
                )
                self.assertFalse(c1.just_woken_up)
                self.assertGreater(c1.last_activity_time, (time.time() - 5.0) * 1000.0)
                await asyncio.sleep(0)
                self.assertTrue(c1.vad_resume_task.cancelled())

                # 3. Non-Mei TTS (EdgeTTS) wakeup works even when config wakeup_words has punctuation / mixed case
                class _FakeEdgeTTS:
                    voice = "zh-CN-XiaoxiaoNeural"
                    ref_audio_path = ""

                    def to_tts(self, _text):
                        return None

                c1.tts = _FakeEdgeTTS()
                with (
                    patch.object(hello_mod, "audio_to_data", return_value=[b"\x00\x01"]),
                    patch.object(hello_mod, "sendAudioMessage", return_value=None),
                    patch.object(hello_mod, "send_tts_message", return_value=None),
                    patch.object(hello_mod, "_finish_wakeup_reply", return_value=None),
                    patch.object(hello_mod, "wakeupWordsResponse", return_value=None),
                ):
                    from core.utils.dialogue import Dialogue
                    c1.dialogue = Dialogue()
                    wk1, _ = pc_pcm_input.match_pc_wakeup_word(c1, "你好芽衣")
                    self.assertTrue(await hello_mod.checkWakeupWords(c1, wk1))
                    wk2, _ = pc_pcm_input.match_pc_wakeup_word(c1, "hey mei")
                    self.assertTrue(await hello_mod.checkWakeupWords(c1, wk2))

                # 4. Saving idle-farewell timeout via 18080 config route preserves standby timeout_seconds >= 86400
                await pc_pcm_input.enter_pc_wake_standby(c1, reason="client_start")
                self.assertGreaterEqual(c1.timeout_seconds, 86400)
                with patch.object(
                    config_routes.runtime_settings,
                    "set_idle_farewell",
                    return_value={"enabled": True, "mode": "fixed", "timeout_seconds": 45, "messages": ["bye"]},
                ):
                    ok_upd, _ = config_routes._update_idle_farewell_config({"timeout_seconds": 45})
                self.assertTrue(ok_upd)
                self.assertGreaterEqual(c1.timeout_seconds, 86400)
                self.assertEqual(getattr(c1, "_pc_wake_prev_timeout_seconds", 0), 105)

                # 5. Multi-tab PC mic preemption supersedes older active PC mic tab without touching text-only PCM
                text_only_pcm = _make_pc_conn("text-only", loop)
                runtime_events.register_connection(text_only_pcm)
                c2 = _make_pc_conn("c2", loop)
                runtime_events.register_connection(c2)
                await lh.handle(c2, {"type": "listen", "state": "start", "mode": "auto", "pc_wake_mode": True})
                await asyncio.sleep(0.05)

                self.assertFalse(pc_pcm_input.is_pc_mic_connection_active(c1))
                self.assertTrue(c1.websocket.closed)
                c1_states = [m for m in c1.websocket.json_messages() if m.get("type") == "pc_mic_state"]
                self.assertEqual(c1_states[-1]["state"], "off")
                self.assertEqual(c1_states[-1]["reason"], "replaced_by_new_pc_mic")
                self.assertTrue(pc_pcm_input.is_pc_mic_connection_active(c2))
                self.assertFalse(text_only_pcm.websocket.closed)

                # 6. Full boot ASR wrapper stack MUST NOT double-apply chaining homophone corrections on awake non-Sherpa ASR
                c2.asr = _FakeDoubaoASR()
                await pc_pcm_input.enter_pc_wake_awake(c2, wake_word="你好芽衣")
                c2.asr.next_text = "芽衣你好"
                chat_texts: list[str] = []

                async def _capture_chat(_conn, txt):
                    chat_texts.append(txt)

                with tempfile.TemporaryDirectory() as td:
                    corr_p = os.path.join(td, "corr.json")
                    asr_corrections.save_asr_corrections({"芽衣": "雷电芽衣"}, enabled=True, path=corr_p)
                    with (
                        patch.object(asr_corrections, "ASR_CORRECTIONS_PATH", corr_p),
                        patch("core.providers.asr.base.startToChat", side_effect=_capture_chat),
                    ):
                        await c2.asr.handle_voice_stop(c2, [b"\x01\x02" * 960 * 16])
                self.assertEqual(c2.asr.calls, 1)
                self.assertEqual(chat_texts, ["雷电芽衣你好"])

        asyncio.run(_scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
