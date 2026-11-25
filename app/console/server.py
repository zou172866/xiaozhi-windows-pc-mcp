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

import persona_versions
import proactive_companion
import runtime_events
import voiceprint_router
from console import shared
from console.routes import (
    anima,
    archives,
    audio_api,
    config,
    gsv,
    l3,
    logs,
    mobile_push as mobile_push_routes,
    pc_mic,
    proactive,
    state,
    tavern,
    voiceprint,
)

_console_atexit_registered = False

_STATIC_ASSETS = {
    "/static/app.css": ("CSS_PATH", "text/css; charset=utf-8"),
    "/static/app.js": ("JS_PATH", "application/javascript; charset=utf-8"),
    "/static/pcm_worklet.js": ("WORKLET_JS_PATH", "application/javascript; charset=utf-8"),
}


class ConsoleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def parse_request(self):
        self._security_allowed = False
        return super().parse_request()

    def end_headers(self):
        allowed = bool(getattr(self, "_security_allowed", False))
        self._security_allowed = False
        if allowed:
            hdrs = getattr(self, "headers", None)
            if hdrs is not None:
                if hasattr(hdrs, "get_all"):
                    raw_origins = hdrs.get_all("Origin") or []
                elif hasattr(hdrs, "items"):
                    raw_origins = [v for k, v in hdrs.items() if str(k).lower() == "origin"]
                else:
                    raw_origins = []
                if len(raw_origins) == 1:
                    origin_low = str(raw_origins[0] or "").strip().lower()
                    if origin_low and origin_low in shared.get_allowed_console_origins(shared.CONSOLE_PORT):
                        self.send_header("Access-Control-Allow-Origin", origin_low)
                        self.send_header("Vary", "Origin")
                        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Console-Token")
                        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob: data:; font-src 'self' data:; "
            f"connect-src 'self' ws://127.0.0.1:{shared.CONSOLE_PORT} ws://localhost:{shared.CONSOLE_PORT}; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_json(self, data, status=HTTPStatus.OK, *, close_connection: bool = False):
        safe_data = shared._redact_json_payload(data)
        body = json.dumps(safe_data, ensure_ascii=False).encode("utf-8")
        if close_connection:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_body_json(self, *, max_bytes: int = 2 * 1024 * 1024) -> dict:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except (TypeError, ValueError) as exc:
            raise shared._BadRequestError("无效的 Content-Length 请求头") from exc
        cap = max(1024, int(max_bytes))
        if length < 0 or length > cap:
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
        self._security_allowed = False
        parsed_path = urlparse(self.path).path
        ok, status, err = shared.validate_console_request(
            method, self.headers, port=shared.CONSOLE_PORT, path=parsed_path
        )
        if not ok:
            has_unread_body = (
                str(method).upper() == "POST"
                or bool(self.headers.get("Content-Length"))
                or bool(self.headers.get("Transfer-Encoding"))
            )
            self._send_json(
                {"ok": False, "error": err},
                HTTPStatus(status),
                close_connection=has_unread_body,
            )
            return False
        self._security_allowed = True
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

    def do_OPTIONS(self):
        if not self._enforce_security("OPTIONS"):
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

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

        if path == "/ws/pc-mic":
            pc_mic.handle_ws_pc_mic(self, parsed)
            return

        if path == "/api/state":
            state.handle_get_state(self, parsed)
            return

        if path == "/api/persona/history":
            config.handle_get_persona_history(self, parsed)
            return

        if path == "/api/dream":
            anima.handle_get_dream(self, parsed)
            return

        if path == "/api/memory-archive":
            archives.handle_get_memory_archive(self, parsed)
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
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST, close_connection=True)

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
    "/api/recall-style": config.handle_get_recall_style,
    "/api/esp32-devices": config.handle_get_esp32_devices,
    "/api/audio-providers": audio_api.handle_get_audio_providers,
    "/api/proactive": proactive.handle_get_proactive,
    "/api/voiceprint": voiceprint.handle_get_voiceprint,
    "/api/companion-widget": voiceprint.handle_get_companion_widget,
    "/api/mobile-push": mobile_push_routes.handle_get_mobile_push,
}

_POST_ROUTES = {
    "/api/memory-archive": archives.handle_post_memory_archive,
    "/api/tavern-handoff": tavern.handle_post_handoff,
    "/api/pc-mode": l3.handle_post_pc_mode,
    "/api/audio-output": state.handle_post_audio_output,
    "/api/reply-length": config.handle_post_reply_length,
    "/api/idle-farewell": config.handle_post_idle_farewell,
    "/api/anima-outbox/replay-dead": anima.handle_post_anima_outbox_replay_dead,
    "/api/vad-speculative": config.handle_post_vad_speculative,
    "/api/asr-corrections": config.handle_post_asr_corrections,
    "/api/recall-style": config.handle_post_recall_style,
    "/api/esp32-devices": config.handle_post_esp32_devices,
    "/api/persona/rollback": config.handle_post_persona_rollback,
    "/api/dream/run": anima.handle_post_dream_run,
    "/api/dream/config": anima.handle_post_dream_config,
    "/api/dream/approve": anima.handle_post_dream_approve,
    "/api/dream/reject": anima.handle_post_dream_reject,
    "/api/embedding/reset-breaker": anima.handle_post_embedding_reset_breaker,
    "/api/asr-test": audio_api.handle_post_asr_test,
    "/api/relationship": anima.handle_post_relationship,
    "/api/obsidian/config": anima.handle_post_obsidian_config,
    "/api/obsidian/rebuild": anima.handle_post_obsidian_rebuild,
    "/api/obsidian/briefing": anima.handle_post_obsidian_briefing,
    "/api/obsidian/inbox/scan": anima.handle_post_obsidian_inbox_scan,
    "/api/l3-backend": l3.handle_post_l3_backend,
    "/api/audio-test": gsv.handle_post_audio_test,
    "/api/clear-history": state.handle_post_clear_history,
    "/api/tts-preview": gsv.handle_post_tts_preview,
    "/api/proactive": proactive.handle_post_proactive,
    "/api/proactive/run": proactive.handle_post_proactive_run,
    "/api/proactive/inner-life": proactive.handle_post_proactive_inner_life,
    "/api/voiceprint/config": voiceprint.handle_post_voiceprint_config,
    "/api/voiceprint/enroll-last": voiceprint.handle_post_voiceprint_enroll_last,
    "/api/voiceprint/enroll-wav": voiceprint.handle_post_voiceprint_enroll_wav,
    "/api/voiceprint/delete": voiceprint.handle_post_voiceprint_delete,
    "/api/voiceprint/download-model": voiceprint.handle_post_voiceprint_download_model,
    "/api/screen-peek": voiceprint.handle_post_screen_peek,
    "/api/mobile-push/config": mobile_push_routes.handle_post_mobile_push_config,
    "/api/mobile-push/test": mobile_push_routes.handle_post_mobile_push_test,
    "/api/mobile-push/briefing": mobile_push_routes.handle_post_mobile_push_briefing,
    "/api/pc-mic/session": pc_mic.handle_post_pc_mic_session,
}


def _flush_console_state_on_shutdown() -> None:
    """Flush coalesced dirty history and throttled log cursor on clean process shutdown (D-1 / D-4)."""
    try:
        proactive_companion.stop_proactive_companion(timeout=1.5)
    except Exception as exc:
        shared._log_rate_limited("shutdown_stop_proactive", exc)
    try:
        voiceprint_router.unload_voiceprint_model(run_gc=False)
    except Exception as exc:
        shared._log_rate_limited("shutdown_unload_voiceprint", exc)
    try:
        shared.flush_history_if_dirty()
    except Exception as exc:
        shared._log_rate_limited("shutdown_flush_history", exc)
    try:
        logs.flush_log_cursor_if_dirty()
    except Exception as exc:
        shared._log_rate_limited("shutdown_flush_cursor", exc)


def _run_boot_sync(*, sync_persona: bool = True, sync_asr: bool = True) -> tuple[bool, bool]:
    """Sync initial persona baseline revision and local ASR corrections to Anima Brain on startup."""
    persona_ok = not sync_persona
    asr_ok = not sync_asr
    if sync_persona:
        try:
            cfg = shared._read_raw_yaml()
            raw_prompt = str(cfg.get("prompt") or "").strip()
            if raw_prompt:
                st = persona_versions.ensure_synced_with_prompt(
                    raw_prompt,
                    char_name=anima._infer_character_name(raw_prompt),
                )
                rev = int(st.get("current_revision") or 0) or None
                dig = str(st.get("current_digest") or "")
                persona_ok = bool(anima._sync_persona_to_anima(raw_prompt, revision=rev, digest=dig))
            else:
                persona_ok = True
        except Exception as exc:
            shared._log_rate_limited("boot_sync_persona", exc)
            persona_ok = False
    if sync_asr:
        try:
            asr_ok = bool(anima._sync_asr_corrections_to_anima())
        except Exception as exc:
            shared._log_rate_limited("boot_sync_asr", exc)
            asr_ok = False
    return persona_ok, asr_ok


def start_console_background() -> None:
    global _console_atexit_registered
    if not _console_atexit_registered:
        atexit.register(_flush_console_state_on_shutdown)
        _console_atexit_registered = True

    shared._load_history()
    shared.persist_console_token()
    runtime_events.subscribe(shared._apply_runtime_event)
    shared._sync_runtime_events()
    shared.start_history_flusher()
    watcher = threading.Thread(target=logs._log_watcher_loop, name="console-log-watcher", daemon=True)
    watcher.start()
    prober = threading.Thread(target=state._service_probe_loop, name="console-service-prober", daemon=True)
    prober.start()
    try:
        proactive_companion.start_proactive_companion()
    except Exception as exc:
        shared._log_rate_limited("start_proactive_companion", exc)

    def _boot_sync():
        need_persona = True
        need_asr = True
        for delay in (2.0, 4.0, 8.0, 15.0):
            time.sleep(delay)
            p_ok, a_ok = _run_boot_sync(sync_persona=need_persona, sync_asr=need_asr)
            if p_ok:
                need_persona = False
            if a_ok:
                need_asr = False
            if not need_persona and not need_asr:
                break

    threading.Thread(target=_boot_sync, name="anima-persona-sync", daemon=True).start()

    def _serve():
        httpd = ThreadingHTTPServer(("0.0.0.0", shared.CONSOLE_PORT), ConsoleHandler)
        print(f"[WebConsole] 本地可视化控制台已启动: http://localhost:{shared.CONSOLE_PORT}", flush=True)
        httpd.serve_forever()

    srv_thread = threading.Thread(target=_serve, name="console-http-server", daemon=True)
    srv_thread.start()
