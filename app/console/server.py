"""HTTP routing server and background lifecycle manager for the 18080 Web Console (H-1).

Zero import-time side effects: no background threads or `atexit` hooks are registered
until `start_console_background()` is explicitly invoked by `boot.main()`.
"""

from __future__ import annotations

import atexit
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from urllib.parse import urlparse

import runtime_events
from console import shared
from console.routes import anima, config, gsv, l3, logs, state

_console_atexit_registered = False

_STATIC_ASSETS = {
    "/static/app.css": ("CSS_PATH", "text/css; charset=utf-8"),
    "/static/app.js": ("JS_PATH", "application/javascript; charset=utf-8"),
}


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def end_headers(self):
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            p = urlparse(origin)
            if p.scheme in ("http", "https") and (p.hostname or "").lower() in ("localhost", "127.0.0.1"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Console-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_json(self, data, status=HTTPStatus.OK):
        safe_data = shared._redact_json_payload(data)
        body = json.dumps(safe_data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body_json(self) -> dict:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except (TypeError, ValueError) as exc:
            raise shared._BadRequestError("无效的 Content-Length 请求头") from exc
        if length < 0 or length > 2 * 1024 * 1024:
            raise shared._BadRequestError("请求体过大或长度非法")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise shared._BadRequestError(f"请求体不是合法的 UTF-8 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise shared._BadRequestError("请求体必须是 JSON 对象")
        return parsed

    def _enforce_security(self, method: str) -> bool:
        parsed_path = urlparse(self.path).path
        ok, status, err = shared.validate_console_request(
            method, self.headers, port=shared.CONSOLE_PORT, path=parsed_path
        )
        if not ok:
            self._send_json({"ok": False, "error": err}, HTTPStatus(status))
            return False
        return True

    def _serve_static_file(self, path_attr: str, content_type: str) -> None:
        fp = getattr(shared, path_attr)
        if not os.path.exists(fp):
            self._send_json({"error": f"{os.path.basename(fp)} not found"}, HTTPStatus.NOT_FOUND)
            return
        with open(fp, "rb") as f:
            raw = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._enforce_security("GET"):
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not os.path.exists(shared.HTML_PATH):
                self._send_json({"error": "index.html not found"}, HTTPStatus.NOT_FOUND)
                return
            with open(shared.HTML_PATH, "r", encoding="utf-8") as f:
                html_text = f.read()
            html_text = html_text.replace(shared._CONSOLE_TOKEN_PLACEHOLDER, shared._CONSOLE_WRITE_TOKEN)
            html_bytes = html_text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path in _STATIC_ASSETS:
            attr_name, ctype = _STATIC_ASSETS[path]
            self._serve_static_file(attr_name, ctype)
            return

        if path == "/api/state":
            state.handle_get_state(self, parsed)
            return

        get_route = _GET_ROUTES.get(path)
        if get_route is not None:
            get_route(self)
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._enforce_security("POST"):
            return
        parsed = urlparse(self.path)
        try:
            self._handle_post(parsed)
        except shared._BadRequestError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _handle_post(self, parsed):
        path = parsed.path
        if path == "/api/config":
            config.handle_post_config(self, shutdown_flush_fn=_flush_console_state_on_shutdown)
            return
        post_route = _POST_ROUTES.get(path)
        if post_route is not None:
            post_route(self)
            return
        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


_GET_ROUTES = {
    "/api/health": state.handle_get_health,
    "/api/relationship": anima.handle_get_relationship,
    "/api/obsidian": anima.handle_get_obsidian,
    "/api/l3-backend": l3.handle_get_l3_backend,
    "/api/idle-farewell": config.handle_get_idle_farewell,
    "/api/anima-outbox": anima.handle_get_anima_outbox,
    "/api/vad-speculative": config.handle_get_vad_speculative,
    "/api/asr-corrections": config.handle_get_asr_corrections,
}

_POST_ROUTES = {
    "/api/pc-mode": l3.handle_post_pc_mode,
    "/api/audio-output": state.handle_post_audio_output,
    "/api/reply-length": config.handle_post_reply_length,
    "/api/idle-farewell": config.handle_post_idle_farewell,
    "/api/anima-outbox/replay-dead": anima.handle_post_anima_outbox_replay_dead,
    "/api/vad-speculative": config.handle_post_vad_speculative,
    "/api/asr-corrections": config.handle_post_asr_corrections,
    "/api/relationship": anima.handle_post_relationship,
    "/api/obsidian/config": anima.handle_post_obsidian_config,
    "/api/obsidian/rebuild": anima.handle_post_obsidian_rebuild,
    "/api/l3-backend": l3.handle_post_l3_backend,
    "/api/audio-test": gsv.handle_post_audio_test,
    "/api/clear-history": state.handle_post_clear_history,
    "/api/tts-preview": gsv.handle_post_tts_preview,
}


def _flush_console_state_on_shutdown() -> None:
    """Flush coalesced dirty history and throttled log cursor on clean process shutdown (D-1 / D-4)."""
    try:
        shared.flush_history_if_dirty()
    except Exception as exc:
        shared._log_rate_limited("shutdown_flush_history", exc)
    try:
        logs.flush_log_cursor_if_dirty()
    except Exception as exc:
        shared._log_rate_limited("shutdown_flush_cursor", exc)


def start_console_background() -> None:
    global _console_atexit_registered
    if not _console_atexit_registered:
        atexit.register(_flush_console_state_on_shutdown)
        _console_atexit_registered = True

    shared._load_history()
    runtime_events.subscribe(shared._apply_runtime_event)
    shared._sync_runtime_events()
    shared.start_history_flusher()
    watcher = threading.Thread(target=logs._log_watcher_loop, name="console-log-watcher", daemon=True)
    watcher.start()
    prober = threading.Thread(target=state._service_probe_loop, name="console-service-prober", daemon=True)
    prober.start()

    def _boot_sync():
        time.sleep(2.0)
        cfg = shared._read_raw_yaml()
        if cfg.get("prompt"):
            anima._sync_persona_to_anima(str(cfg["prompt"]))

    threading.Thread(target=_boot_sync, name="anima-persona-sync", daemon=True).start()

    def _serve():
        httpd = ThreadingHTTPServer(("0.0.0.0", shared.CONSOLE_PORT), ConsoleHandler)
        print(f"[WebConsole] 本地可视化控制台已启动: http://localhost:{shared.CONSOLE_PORT}", flush=True)
        httpd.serve_forever()

    srv_thread = threading.Thread(target=_serve, name="console-http-server", daemon=True)
    srv_thread.start()
