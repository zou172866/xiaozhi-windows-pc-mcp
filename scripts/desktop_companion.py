"""Standalone zero-extra-dependency desktop corner companion widget (Phase 2 ⑤).

Uses only packages already present in `pc-mcp/.venv` (`tkinter`, `Pillow`, `httpx`) plus stdlib.
Design invariants:
- Independent opt-in launch (`桌角陪伴小窗.bat` -> `data/start_companion_widget.ps1`), zero memory
  or background processes when not launched.
- Authenticated strictly against local loopback `127.0.0.1:18080` via `state/.console_token`
  (`X-Console-Token`) with `trust_env=False`.
- Auto-loads custom character portraits from `voices/<character>/portraits/{emotion,default}.{png,webp,jpg}`
  and falls back to anti-aliased 7-emotion vector avatars when no portrait files are provided.
- Never steals typing focus (`WS_EX_NOACTIVATE` + `WS_EX_TOOLWINDOW`), draggable with saved coordinates,
  double-click collapses to a compact pill, right-click menu provides Screen Peek / Dry-Run / Console / Quit.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Dict, Optional, Tuple

import httpx
from PIL import Image, ImageDraw

SERVER_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SERVER_DIR / "state"
VOICES_DIR = SERVER_DIR / "voices"
DEFAULT_TOKEN_FILE = STATE_DIR / ".console_token"
DEFAULT_POS_FILE = STATE_DIR / "widget_pos.json"
DEFAULT_PID_FILE = STATE_DIR / "desktop_companion.pid"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:18080"

VALID_EMOTIONS = ("calm", "gentle", "happy", "playful", "shy", "serious", "tender")
PORTRAIT_EXTS = (".png", ".webp", ".jpg", ".jpeg")

EMOTION_THEMES: Dict[str, Dict[str, Any]] = {
    "calm": {
        "label_zh": "平静",
        "ring": (139, 124, 214, 255),
        "bg": (34, 31, 52, 255),
        "accent": (182, 168, 245, 255),
    },
    "gentle": {
        "label_zh": "温柔",
        "ring": (168, 130, 232, 255),
        "bg": (40, 32, 56, 255),
        "accent": (214, 186, 255, 255),
    },
    "happy": {
        "label_zh": "开心",
        "ring": (244, 178, 92, 255),
        "bg": (48, 38, 52, 255),
        "accent": (255, 212, 136, 255),
    },
    "playful": {
        "label_zh": "调皮",
        "ring": (236, 120, 178, 255),
        "bg": (46, 32, 54, 255),
        "accent": (255, 170, 212, 255),
    },
    "shy": {
        "label_zh": "害羞",
        "ring": (242, 132, 152, 255),
        "bg": (50, 32, 48, 255),
        "accent": (255, 182, 196, 255),
    },
    "serious": {
        "label_zh": "认真",
        "ring": (92, 168, 236, 255),
        "bg": (28, 36, 54, 255),
        "accent": (146, 204, 255, 255),
    },
    "tender": {
        "label_zh": "安抚",
        "ring": (196, 146, 224, 255),
        "bg": (44, 32, 52, 255),
        "accent": (234, 196, 248, 255),
    },
}

PC_MODE_ZH = {"off": "只聊挡", "safe": "安全挡", "full": "全权挡"}


def _is_loopback_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").strip().lower().strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def load_console_token(token_file: Path = DEFAULT_TOKEN_FILE) -> str:
    try:
        if token_file.is_file():
            return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _safe_char_dir_name(character_key: str) -> str:
    cleaned = "".join(ch for ch in str(character_key or "").strip().lower() if ch.isalnum() or ch in ("_", "-"))
    return cleaned or "raiden_mei"


def resolve_portrait_path(
    voices_dir: Path,
    character_key: str,
    emotion: str,
) -> Optional[Path]:
    """Locate `voices/<character>/portraits/{emotion,default}.{png,webp,jpg}` safely."""
    char_dir = _safe_char_dir_name(character_key)
    emo = str(emotion or "calm").strip().lower()
    if emo not in EMOTION_THEMES:
        emo = "calm"
    base_portraits = (Path(voices_dir) / char_dir / "portraits").resolve()
    try:
        base_portraits.relative_to(Path(voices_dir).resolve())
    except ValueError:
        return None
    if not base_portraits.is_dir():
        return None

    for stem in (emo, "default"):
        for ext in PORTRAIT_EXTS:
            cand = base_portraits / f"{stem}{ext}"
            if cand.is_file():
                return cand
    return None


def _draw_vector_face(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, emo: str, accent: Tuple[int, int, int, int]) -> None:
    """Draw a minimal, crisp 7-emotion anime expression onto supersampled canvas."""
    eye_dx = int(r * 0.30)
    eye_y = int(cy - r * 0.08)
    eye_r = max(2, int(r * 0.09))
    stroke = max(2, int(r * 0.06))
    blush = (248, 146, 166, 150)

    if emo == "happy":
        # Smiling curved eyes (^ ^)
        for sx in (-eye_dx, eye_dx):
            box = [cx + sx - eye_r * 2, eye_y - eye_r, cx + sx + eye_r * 2, eye_y + eye_r * 2]
            draw.arc(box, start=195, end=345, fill=accent, width=stroke)
        mouth = [cx - int(r * 0.22), cy + int(r * 0.06), cx + int(r * 0.22), cy + int(r * 0.34)]
        draw.arc(mouth, start=15, end=165, fill=accent, width=stroke)
    elif emo == "playful":
        # Left open eye, right wink
        draw.ellipse([cx - eye_dx - eye_r, eye_y - eye_r, cx - eye_dx + eye_r, eye_y + eye_r], fill=accent)
        wbox = [cx + eye_dx - eye_r * 2, eye_y - eye_r, cx + eye_dx + eye_r * 2, eye_y + eye_r * 2]
        draw.arc(wbox, start=195, end=345, fill=accent, width=stroke)
        mouth = [cx - int(r * 0.18), cy + int(r * 0.08), cx + int(r * 0.22), cy + int(r * 0.30)]
        draw.arc(mouth, start=15, end=155, fill=accent, width=stroke)
    elif emo == "shy":
        for sx in (-eye_dx, eye_dx):
            draw.ellipse([cx + sx - eye_r, eye_y - eye_r, cx + sx + eye_r, eye_y + eye_r], fill=accent)
            # Soft blush cheeks
            bx = cx + int(sx * 1.28)
            by = cy + int(r * 0.12)
            draw.ellipse([bx - eye_r * 2, by - eye_r, bx + eye_r * 2, by + eye_r], fill=blush)
        mouth = [cx - int(r * 0.12), cy + int(r * 0.14), cx + int(r * 0.12), cy + int(r * 0.26)]
        draw.arc(mouth, start=20, end=160, fill=accent, width=max(2, stroke - 1))
    elif emo == "serious":
        for sx in (-eye_dx, eye_dx):
            draw.ellipse([cx + sx - eye_r, eye_y - eye_r, cx + sx + eye_r, eye_y + eye_r], fill=accent)
        # Determined brows
        draw.line(
            [(cx - eye_dx - eye_r * 2, eye_y - eye_r * 2), (cx - eye_dx + eye_r, eye_y - eye_r)],
            fill=accent,
            width=stroke,
        )
        draw.line(
            [(cx + eye_dx + eye_r * 2, eye_y - eye_r * 2), (cx + eye_dx - eye_r, eye_y - eye_r)],
            fill=accent,
            width=stroke,
        )
        draw.line([(cx - int(r * 0.16), cy + int(r * 0.22)), (cx + int(r * 0.16), cy + int(r * 0.22))], fill=accent, width=stroke)
    elif emo == "tender":
        for sx in (-eye_dx, eye_dx):
            box = [cx + sx - eye_r * 2, eye_y - eye_r, cx + sx + eye_r * 2, eye_y + eye_r * 2]
            draw.arc(box, start=20, end=160, fill=accent, width=stroke)
        mouth = [cx - int(r * 0.16), cy + int(r * 0.10), cx + int(r * 0.16), cy + int(r * 0.28)]
        draw.arc(mouth, start=20, end=160, fill=accent, width=stroke)
    elif emo == "gentle":
        for sx in (-eye_dx, eye_dx):
            draw.ellipse([cx + sx - eye_r, eye_y - eye_r, cx + sx + eye_r, eye_y + eye_r], fill=accent)
        mouth = [cx - int(r * 0.18), cy + int(r * 0.08), cx + int(r * 0.18), cy + int(r * 0.28)]
        draw.arc(mouth, start=18, end=162, fill=accent, width=stroke)
    else:  # calm
        for sx in (-eye_dx, eye_dx):
            draw.ellipse([cx + sx - eye_r, eye_y - eye_r, cx + sx + eye_r, eye_y + eye_r], fill=accent)
        mouth = [cx - int(r * 0.14), cy + int(r * 0.14), cx + int(r * 0.14), cy + int(r * 0.24)]
        draw.arc(mouth, start=25, end=155, fill=accent, width=max(2, stroke - 1))


def render_avatar_image(
    size: int,
    emotion: str,
    *,
    portrait_path: Optional[Path] = None,
    speaking: bool = False,
) -> Image.Image:
    """Render an anti-aliased RGBA circular avatar (portrait if provided, else 7-emotion vector fallback)."""
    sz = max(32, int(size))
    ss = sz * 2  # 2x supersampling for crisp edges
    emo = str(emotion or "calm").strip().lower()
    theme = EMOTION_THEMES.get(emo, EMOTION_THEMES["calm"])
    ring_col = theme["ring"]
    bg_col = theme["bg"]
    accent_col = theme["accent"]

    canvas = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    pad = 8 if speaking else 4
    if speaking:
        glow = (ring_col[0], ring_col[1], ring_col[2], 95)
        draw.ellipse([2, 2, ss - 3, ss - 3], outline=glow, width=6)

    ring_w = 8
    inner_box = [pad + ring_w, pad + ring_w, ss - pad - ring_w, ss - pad - ring_w]
    inner_w = max(8, inner_box[2] - inner_box[0])

    portrait_loaded = False
    if portrait_path is not None and Path(portrait_path).is_file():
        try:
            with Image.open(portrait_path) as src:
                img = src.convert("RGBA")
                w, h = img.size
                side = min(w, h)
                left = (w - side) // 2
                top = (h - side) // 2
                cropped = img.crop((left, top, left + side, top + side)).resize(
                    (inner_w, inner_w), Image.Resampling.LANCZOS
                )
            mask = Image.new("L", (inner_w, inner_w), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, inner_w - 1, inner_w - 1], fill=255)
            canvas.paste(cropped, (inner_box[0], inner_box[1]), mask)
            portrait_loaded = True
        except Exception:
            portrait_loaded = False

    if not portrait_loaded:
        draw.ellipse(inner_box, fill=bg_col)
        cx = ss // 2
        cy = ss // 2
        _draw_vector_face(draw, cx, cy, inner_w // 2, emo, accent_col)

    draw.ellipse([pad, pad, ss - pad - 1, ss - pad - 1], outline=ring_col, width=ring_w)
    return canvas.resize((sz, sz), Image.Resampling.LANCZOS)


def choose_display_bubble(state: Dict[str, Any]) -> Tuple[str, str]:
    """Pick the most relevant recent bubble text and tag from `/api/companion-widget` state."""
    if not isinstance(state, dict):
        return "", ""
    cands = []
    sub = str(state.get("subtitle") or "").strip()
    if sub:
        cands.append((float(state.get("subtitle_ts") or 0.0), "对话", sub))
    pro = state.get("proactive_bubble")
    if isinstance(pro, dict) and str(pro.get("text") or "").strip():
        tag = "主动关心" if pro.get("delivered") else "试跑台词"
        cands.append((float(pro.get("ts") or 0.0), tag, str(pro["text"]).strip()))
    peek = state.get("screen_peek_bubble")
    if isinstance(peek, dict) and str(peek.get("text") or "").strip():
        cands.append((float(peek.get("ts") or 0.0), "浅看屏", str(peek["text"]).strip()))
    if not cands:
        return "", ""
    cands.sort(key=lambda x: x[0], reverse=True)
    _, tag, text = cands[0]
    return tag, text


def save_widget_position(pos_path: Path, x: int, y: int) -> None:
    pos_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pos_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"x": int(x), "y": int(y)}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, pos_path)


def load_widget_position(pos_path: Path) -> Optional[Tuple[int, int]]:
    try:
        if not pos_path.is_file():
            return None
        data = json.loads(pos_path.read_text(encoding="utf-8"))
        if (
            isinstance(data, dict)
            and isinstance(data.get("x"), int)
            and not isinstance(data.get("x"), bool)
            and isinstance(data.get("y"), int)
            and not isinstance(data.get("y"), bool)
        ):
            return int(data["x"]), int(data["y"])
    except Exception:
        pass
    return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or int(code.value) != 259:
                return False
            # Guard against recycled Windows PIDs belonging to unrelated non-Python processes
            buf_len = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                exe_name = Path(buf.value).name.lower()
                if "python" not in exe_name:
                    return False
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_single_instance_pid(pid_path: Path = DEFAULT_PID_FILE) -> bool:
    """Prevent duplicate desktop widget windows via a simple PID file check."""
    try:
        if pid_path.is_file():
            old_pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
            if old_pid > 0 and old_pid != os.getpid() and _pid_is_alive(old_pid):
                return False
    except Exception:
        pass
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return True


def release_single_instance_pid(pid_path: Path = DEFAULT_PID_FILE) -> None:
    try:
        if pid_path.is_file():
            cur = int(pid_path.read_text(encoding="utf-8").strip() or "0")
            if cur == os.getpid():
                pid_path.unlink(missing_ok=True)
    except Exception:
        pass


class WidgetApiClient:
    """Loopback-only authenticated HTTP client for 18080 `/api/companion-widget`."""

    def __init__(self, base_url: str = DEFAULT_CONSOLE_URL, token_file: Path = DEFAULT_TOKEN_FILE) -> None:
        if not _is_loopback_url(base_url):
            raise ValueError(f"Companion widget refuses non-loopback console URL: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self._client = httpx.Client(trust_env=False, timeout=2.5, follow_redirects=False)

    def _headers(self) -> Dict[str, str]:
        tok = load_console_token(self.token_file)
        hdrs = {"Accept": "application/json"}
        if tok:
            hdrs["X-Console-Token"] = tok
        return hdrs

    def fetch_state(self) -> Optional[Dict[str, Any]]:
        try:
            resp = self._client.get(f"{self.base_url}/api/companion-widget", headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else None
        except Exception:
            pass
        return None

    def post_action(self, path: str, payload: Dict[str, Any], timeout: float = 20.0) -> Tuple[bool, Dict[str, Any]]:
        try:
            hdrs = self._headers()
            hdrs["Content-Type"] = "application/json"
            resp = self._client.post(f"{self.base_url}{path}", headers=hdrs, json=payload, timeout=timeout)
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                data = {}
            return (200 <= resp.status_code < 300 and bool(data.get("ok", True))), data
        except Exception as exc:
            return False, {"ok": False, "error": str(exc)}

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class WidgetMicController:
    """Opt-in desktop microphone controller for the corner companion widget (default OFF, zero idle cost)."""

    def __init__(
        self,
        *,
        console_url: str = DEFAULT_CONSOLE_URL,
        token_file: Path = DEFAULT_TOKEN_FILE,
        on_event=None,
    ) -> None:
        self._console_url = console_url
        self._token_file = token_file
        self._on_event = on_event
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._ws = None
        self._bridge = None
        self._rx_thread: Optional[threading.Thread] = None
        self._active = False
        self._full_duplex = False

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def full_duplex(self) -> bool:
        with self._lock:
            return self._full_duplex

    def set_full_duplex(self, enabled: bool) -> bool:
        with self._lock:
            self._full_duplex = bool(enabled)
            bridge = self._bridge
        if bridge is not None:
            bridge.set_full_duplex(self._full_duplex)
        return self._full_duplex

    def _emit(self, kind: str, text: str = "") -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, text)
            except Exception:
                pass

    def start_async(self) -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
            self._stop_evt.clear()
        self._emit("connecting", "正在打开电脑麦克风并连接语音通道…")

        def _boot() -> None:
            ws = None
            bridge = None
            try:
                scripts_dir = str(Path(__file__).resolve().parent)
                if scripts_dir not in sys.path:
                    sys.path.insert(0, scripts_dir)
                import pc_talk

                sess = pc_talk.negotiate_ota_session(
                    ota_url=pc_talk.DEFAULT_OTA_URL,
                    console_url=self._console_url,
                    device_id=pc_talk.DEFAULT_DEVICE_ID,
                    client_id="xiaozhi-desktop-widget",
                    token_file=self._token_file,
                    auto_allow=True,
                    ensure_pc_audio=True,
                )
                if self._stop_evt.is_set():
                    return
                ws, _sid = pc_talk.open_xiaozhi_ws_session(sess, audio_format="pcm")
                if self._stop_evt.is_set():
                    ws.close()
                    return
                bridge = pc_talk.PcMicBridge(
                    ws,
                    full_duplex=self.full_duplex,
                    on_error=lambda exc: self._emit("error", f"麦克风异常：{exc}"),
                )
                bridge.start()
                with self._lock:
                    if self._stop_evt.is_set():
                        self._active = False
                        bridge.stop()
                        ws.close()
                        return
                    self._ws = ws
                    self._bridge = bridge
                self._emit("listening", "已开麦聆听中 · 直接说话即可")
                self._rx_loop(ws, bridge)
            except Exception as exc:
                if bridge is not None:
                    try:
                        bridge.stop()
                    except Exception:
                        pass
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                with self._lock:
                    self._ws = None
                    self._bridge = None
                    self._active = False
                if not self._stop_evt.is_set():
                    self._emit("error", f"开麦失败：{exc}")

        t = threading.Thread(target=_boot, name="widget-pc-mic", daemon=True)
        with self._lock:
            self._rx_thread = t
        t.start()

    def _rx_loop(self, ws, bridge) -> None:
        try:
            while not self._stop_evt.is_set():
                try:
                    op, payload = ws.recv_message(timeout=0.8)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if op == 0x8:
                    break
                if op != 0x1:
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "stt":
                    txt = str(msg.get("text") or "").strip()
                    if txt:
                        self._emit("stt", txt)
                elif mtype == "tts":
                    st = str(msg.get("state") or "")
                    seg = str(msg.get("text") or "").strip()
                    bridge.notify_tts_state(st)
                    if st == "sentence_start" and seg:
                        self._emit("tts", seg)
                    elif st == "stop":
                        self._emit("listening", "已开麦聆听中 · 直接说话即可")
        finally:
            self.stop(notify=not self._stop_evt.is_set(), reason="语音会话已结束，点击可重新开麦")

    def interrupt(self) -> None:
        with self._lock:
            bridge = self._bridge
        if bridge is not None:
            try:
                bridge.interrupt()
                self._emit("listening", "已打断播报 · 请继续说话")
            except Exception as exc:
                self._emit("error", f"打断失败：{exc}")

    def stop(self, *, notify: bool = True, reason: str = "电脑麦克风已关闭") -> None:
        with self._lock:
            self._stop_evt.set()
            was_active = self._active
            self._active = False
            bridge = self._bridge
            ws = self._ws
            self._bridge = None
            self._ws = None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if notify and was_active:
            self._emit("stopped", reason)


def _apply_windows_no_activate(hwnd: int) -> None:
    """Apply `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` with Win64-safe ctypes and SWP_FRAMECHANGED."""
    if os.name != "nt" or not hwnd:
        return
    try:
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        get_wl = getattr(u32, "GetWindowLongPtrW", u32.GetWindowLongW)
        set_wl = getattr(u32, "SetWindowLongPtrW", u32.SetWindowLongW)
        get_wl.argtypes = [wintypes.HWND, ctypes.c_int]
        get_wl.restype = ctypes.c_ssize_t
        set_wl.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_wl.restype = ctypes.c_ssize_t
        u32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        u32.SetWindowPos.restype = wintypes.BOOL
        h = wintypes.HWND(int(hwnd))
        cur = int(get_wl(h, GWL_EXSTYLE))
        set_wl(h, GWL_EXSTYLE, cur | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        u32.SetWindowPos(
            h,
            None,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    except Exception:
        pass


def _get_virtual_screen_bounds(fallback_w: int, fallback_h: int) -> Tuple[int, int, int, int]:
    """Return `(vx, vy, vx + vw, vy + vh)` supporting multi-monitor negative coordinates."""
    if os.name == "nt":
        try:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            u32.GetSystemMetrics.argtypes = [ctypes.c_int]
            u32.GetSystemMetrics.restype = ctypes.c_int
            vx = int(u32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            vy = int(u32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            vw = int(u32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            vh = int(u32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            if vw > 100 and vh > 100:
                return vx, vy, vx + vw, vy + vh
        except Exception:
            pass
    return 0, 0, max(320, int(fallback_w)), max(240, int(fallback_h))


def run_widget_app(
    *,
    console_url: str = DEFAULT_CONSOLE_URL,
    token_file: Path = DEFAULT_TOKEN_FILE,
    voices_dir: Path = VOICES_DIR,
    pos_file: Path = DEFAULT_POS_FILE,
    pid_file: Path = DEFAULT_PID_FILE,
) -> int:
    if os.name == "nt":
        try:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            if hasattr(u32, "SetProcessDpiAwarenessContext"):
                u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                u32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
                # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
                if not u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)
            else:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    if not acquire_single_instance_pid(pid_file):
        return 0

    api: Optional[WidgetApiClient] = None
    stop_flag = threading.Event()
    try:
        return _run_widget_ui_loop(
            console_url=console_url,
            token_file=token_file,
            voices_dir=voices_dir,
            pos_file=pos_file,
            pid_file=pid_file,
            stop_flag=stop_flag,
        )
    finally:
        stop_flag.set()
        release_single_instance_pid(pid_file)


def _run_widget_ui_loop(
    *,
    console_url: str,
    token_file: Path,
    voices_dir: Path,
    pos_file: Path,
    pid_file: Path,
    stop_flag: threading.Event,
) -> int:
    import tkinter as tk
    from PIL import ImageTk

    api = WidgetApiClient(base_url=console_url, token_file=token_file)

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.96)
    except Exception:
        pass
    root.configure(bg="#191724")

    # Apply non-focus-stealing style once window handle exists
    root.update_idletasks()
    try:
        if os.name == "nt":
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            u32.GetParent.argtypes = [wintypes.HWND]
            u32.GetParent.restype = wintypes.HWND
            parent_h = u32.GetParent(wintypes.HWND(int(root.winfo_id())))
            hwnd = int(parent_h or root.winfo_id())
            _apply_windows_no_activate(hwnd)
    except Exception:
        pass

    collapsed = {"value": False}
    drag_state = {"dx": 0, "dy": 0, "moved": False}
    avatar_cache: Dict[Tuple[str, str, bool, int, str], Any] = {}
    state_lock = threading.Lock()
    latest_state: Dict[str, Any] = {
        "character_key": "raiden_mei",
        "character_name": "雷电芽衣",
        "emotion": "calm",
        "activity": "idle",
        "subtitle": "",
        "pc_mode": "safe",
    }
    online_flag = {"ok": False, "backoff_ms": 2500}
    feedback_hold = {"until": 0.0}
    action_busy = {"value": False}

    # Layout containers
    outer = tk.Frame(root, bg="#191724", highlightbackground="#6e56cf", highlightthickness=1, padx=10, pady=8)
    outer.pack(fill="both", expand=True)

    top_row = tk.Frame(outer, bg="#191724")
    top_row.pack(fill="x")

    avatar_label = tk.Label(top_row, bg="#191724", bd=0, cursor="fleur")
    avatar_label.pack(side="left", padx=(0, 8))

    info_col = tk.Frame(top_row, bg="#191724")
    info_col.pack(side="left", fill="both", expand=True)

    name_row = tk.Frame(info_col, bg="#191724")
    name_row.pack(fill="x")
    title_lbl = tk.Label(
        name_row,
        text="雷电芽衣 · 平静",
        fg="#f5f3ff",
        bg="#191724",
        font=("Microsoft YaHei UI", 10, "bold"),
        anchor="w",
    )
    title_lbl.pack(side="left")

    status_lbl = tk.Label(
        info_col,
        text="连接 18080 中…",
        fg="#a69ec9",
        bg="#191724",
        font=("Microsoft YaHei UI", 8),
        anchor="w",
    )
    status_lbl.pack(fill="x", pady=(1, 3))

    bubble_lbl = tk.Label(
        outer,
        text="双击头像可折叠为迷你浮球 · 右键更多操作",
        fg="#e2dff2",
        bg="#232036",
        font=("Microsoft YaHei UI", 9),
        wraplength=236,
        justify="left",
        anchor="w",
        padx=8,
        pady=6,
    )
    bubble_lbl.pack(fill="x", pady=(6, 6))

    btn_row = tk.Frame(outer, bg="#191724")
    btn_row.pack(fill="x")

    def _set_bubble(text: str, *, accent: bool = False) -> None:
        bubble_lbl.configure(text=text, fg="#fff5d6" if accent else "#e2dff2")

    def _run_bg_action(
        path: str,
        payload: Dict[str, Any],
        pending_msg: str,
        on_done,
        *,
        timeout: float = 24.0,
    ) -> None:
        if action_busy["value"]:
            return
        action_busy["value"] = True
        _set_bubble(pending_msg, accent=True)

        def _work() -> None:
            ok, data = api.post_action(path, payload, timeout=timeout)

            def _finish() -> None:
                action_busy["value"] = False
                feedback_hold["until"] = time.monotonic() + 6.0
                on_done(ok, data)

            if not stop_flag.is_set():
                try:
                    root.after(0, _finish)
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()

    def _on_screen_peek() -> None:
        def _done(ok: bool, data: Dict[str, Any]) -> None:
            if ok:
                txt = str(data.get("text") or data.get("summary") or "看完了。")
                _set_bubble(f"[浅看屏] {txt}", accent=True)
            else:
                _set_bubble(str(data.get("error") or "浅看屏未执行（请在 18080 开启开关）"))
        _run_bg_action(
            "/api/screen-peek",
            {"question": "", "speak": True},
            "正在看一眼当前窗口…",
            _done,
            timeout=45.0,
        )

    def _on_proactive_dry_run() -> None:
        def _done(ok: bool, data: Dict[str, Any]) -> None:
            if ok:
                ent = data.get("entry") or {}
                txt = str(ent.get("text") or f"保持安静（{ent.get('reason') or data.get('reason') or '无需打扰'}）")
                _set_bubble(f"[试跑] {txt}", accent=True)
            else:
                _set_bubble(str(data.get("error") or "试跑失败"))
        _run_bg_action(
            "/api/proactive/run",
            {"force_dry_run": True, "bypass_timing_gates": True},
            "正在评估当前桌面节律…",
            _done,
            timeout=24.0,
        )

    def _open_console() -> None:
        webbrowser.open(console_url)

    mic_btn_holder: Dict[str, Any] = {"btn": None}

    def _on_mic_event(kind: str, text: str) -> None:
        def _apply() -> None:
            btn = mic_btn_holder.get("btn")
            is_on = mic_ctrl.is_active
            if btn is not None:
                btn.configure(
                    text=("闭麦" if is_on else "开麦"),
                    bg=("#0f766e" if is_on else "#2b2644"),
                    fg=("#ffffff" if is_on else "#dcd6f7"),
                )
            if text:
                feedback_hold["until"] = time.monotonic() + (5.0 if kind in ("error", "stopped") else 4.0)
                prefix = {
                    "stt": "你说",
                    "tts": "回复",
                    "error": "麦克风",
                    "stopped": "麦克风",
                    "connecting": "麦克风",
                    "listening": "麦克风",
                }.get(kind, "麦克风")
                _set_bubble(f"[{prefix}] {text}", accent=(kind in ("listening", "stt", "connecting")))

        if not stop_flag.is_set():
            try:
                root.after(0, _apply)
            except Exception:
                pass

    mic_ctrl = WidgetMicController(
        console_url=console_url,
        token_file=token_file,
        on_event=_on_mic_event,
    )

    def _on_toggle_mic() -> None:
        if mic_ctrl.is_active:
            mic_ctrl.stop(notify=True)
        else:
            mic_ctrl.start_async()

    def _on_toggle_mic_duplex() -> None:
        nxt = mic_ctrl.set_full_duplex(not mic_ctrl.full_duplex)
        feedback_hold["until"] = time.monotonic() + 5.0
        _set_bubble(
            "[麦克风] 已切换为耳机全双工（允许随时插话）"
            if nxt
            else "[麦克风] 已切换为外放防回声（播报时自动闭麦）",
            accent=True,
        )

    def _on_mic_interrupt() -> None:
        if mic_ctrl.is_active:
            mic_ctrl.interrupt()

    def _quit_app() -> None:
        stop_flag.set()
        mic_ctrl.stop(notify=False)
        try:
            save_widget_position(pos_file, root.winfo_x(), root.winfo_y())
        except Exception:
            pass
        api.close()
        release_single_instance_pid(pid_file)
        root.destroy()

    for label, cmd in (
        ("开麦", _on_toggle_mic),
        ("看一眼屏幕", _on_screen_peek),
        ("试跑关怀", _on_proactive_dry_run),
        ("控制台", _open_console),
    ):
        b = tk.Button(
            btn_row,
            text=label,
            command=cmd,
            bg="#2b2644",
            fg="#dcd6f7",
            activebackground="#3b345c",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=7,
            pady=3,
            font=("Microsoft YaHei UI", 8),
            cursor="hand2",
        )
        b.pack(side="left", padx=(0, 6))
        if label == "开麦":
            mic_btn_holder["btn"] = b

    # Context menu
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="展开 / 折叠迷你浮球", command=lambda: _toggle_collapse())
    menu.add_command(label="开启 / 关闭电脑麦克风", command=_on_toggle_mic)
    menu.add_command(label="切换外放防回声 / 耳机插话", command=_on_toggle_mic_duplex)
    menu.add_command(label="立即打断小智说话", command=_on_mic_interrupt)
    menu.add_separator()
    menu.add_command(label="让芽衣看一眼屏幕", command=_on_screen_peek)
    menu.add_command(label="立即试跑主动关怀", command=_on_proactive_dry_run)
    menu.add_separator()
    menu.add_command(label="打开 18080 控制台", command=_open_console)
    menu.add_command(label="退出桌角小窗", command=_quit_app)

    def _show_menu(event) -> None:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _get_photo(char_key: str, emo: str, speaking: bool, sz: int):
        p = resolve_portrait_path(voices_dir, char_key, emo)
        key = (char_key, emo, speaking, sz, str(p or ""))
        if key not in avatar_cache:
            if len(avatar_cache) > 24:
                avatar_cache.clear()
            pil_img = render_avatar_image(sz, emo, portrait_path=p, speaking=speaking)
            avatar_cache[key] = ImageTk.PhotoImage(pil_img)
        return avatar_cache[key]

    def _apply_state_ui() -> None:
        with state_lock:
            st = dict(latest_state)
            is_online = bool(online_flag["ok"])
        char_key = str(st.get("character_key") or "raiden_mei")
        char_name = str(st.get("character_name") or "雷电芽衣")
        emo = str(st.get("emotion") or "calm").lower()
        if emo not in EMOTION_THEMES:
            emo = "calm"
        act = str(st.get("activity") or "idle")
        speaking = act in ("speaking", "listening")
        sz = 52 if collapsed["value"] else 64
        photo = _get_photo(char_key, emo, speaking, sz)
        avatar_label.configure(image=photo)
        avatar_label.image = photo

        theme = EMOTION_THEMES[emo]
        ring_hex = "#{:02x}{:02x}{:02x}".format(*theme["ring"][:3])
        outer.configure(highlightbackground=ring_hex)

        if collapsed["value"]:
            info_col.pack_forget()
            bubble_lbl.pack_forget()
            btn_row.pack_forget()
            avatar_label.pack_configure(padx=(0, 0))
            return

        avatar_label.pack_configure(padx=(0, 8))
        info_col.pack(side="left", fill="both", expand=True)
        bubble_lbl.pack(fill="x", pady=(6, 6))
        btn_row.pack(fill="x")

        act_zh = {"speaking": "说话中", "listening": "聆听中"}.get(act, "待机")
        title_lbl.configure(text=f"{char_name} · {theme['label_zh']}（{act_zh}）")

        if not is_online:
            status_lbl.configure(text="18080 控制台未连接（自动重连中…）")
        else:
            parts = [PC_MODE_ZH.get(str(st.get("pc_mode") or "safe"), "安全挡")]
            spk = st.get("speaker")
            if isinstance(spk, dict) and spk.get("display_name"):
                parts.append(f"说话人:{spk['display_name']}")
            vc = st.get("vocal_cue")
            if isinstance(vc, dict) and vc.get("summary_zh"):
                parts.append(str(vc["summary_zh"])[:18])
            status_lbl.configure(text=" · ".join(parts))

        if not action_busy["value"] and time.monotonic() >= feedback_hold["until"]:
            tag, btxt = choose_display_bubble(st)
            if btxt:
                _set_bubble(f"[{tag}] {btxt}")

    def _toggle_collapse(_event=None) -> None:
        collapsed["value"] = not collapsed["value"]
        _apply_state_ui()

    def _on_drag_start(event) -> None:
        drag_state["dx"] = event.x_root - root.winfo_x()
        drag_state["dy"] = event.y_root - root.winfo_y()
        drag_state["moved"] = False

    def _on_drag_motion(event) -> None:
        nx = event.x_root - drag_state["dx"]
        ny = event.y_root - drag_state["dy"]
        root.geometry(f"+{nx}+{ny}")
        drag_state["moved"] = True

    def _on_drag_end(_event) -> None:
        if drag_state["moved"]:
            try:
                save_widget_position(pos_file, root.winfo_x(), root.winfo_y())
            except Exception:
                pass

    for w in (root, outer, top_row, avatar_label, info_col, name_row, title_lbl, status_lbl, bubble_lbl):
        w.bind("<Button-1>", _on_drag_start)
        w.bind("<B1-Motion>", _on_drag_motion)
        w.bind("<ButtonRelease-1>", _on_drag_end)
        w.bind("<Button-3>", _show_menu)
    avatar_label.bind("<Double-Button-1>", _toggle_collapse)

    # Restore saved coordinates (supports negative multi-monitor coords) or dock cleanly at bottom-right
    _apply_state_ui()
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    ww = max(260, root.winfo_reqwidth())
    wh = max(140, root.winfo_reqheight())
    vx0, vy0, vx1, vy1 = _get_virtual_screen_bounds(sw, sh)
    saved_pos = load_widget_position(pos_file)
    if saved_pos is not None:
        x0 = max(vx0, min(vx1 - 64, saved_pos[0]))
        y0 = max(vy0, min(vy1 - 64, saved_pos[1]))
    else:
        x0 = max(16, sw - ww - 24)
        y0 = max(16, sh - wh - 72)
    root.geometry(f"+{x0}+{y0}")

    def _poll_loop() -> None:
        while not stop_flag.is_set():
            data = api.fetch_state()
            if data and data.get("ok"):
                with state_lock:
                    online_flag["ok"] = True
                    online_flag["backoff_ms"] = 2500
                    latest_state.update(data)
                wait_sec = 0.45 if str(data.get("activity") or "") in ("speaking", "listening") else 2.2
            else:
                with state_lock:
                    online_flag["ok"] = False
                    online_flag["backoff_ms"] = min(8000, int(online_flag["backoff_ms"] * 1.4))
                    wait_sec = online_flag["backoff_ms"] / 1000.0
            if not stop_flag.is_set():
                try:
                    root.after(0, _apply_state_ui)
                except Exception:
                    break
            stop_flag.wait(timeout=wait_sec)

    threading.Thread(target=_poll_loop, name="companion-widget-poll", daemon=True).start()
    root.protocol("WM_DELETE_WINDOW", _quit_app)
    try:
        root.mainloop()
    finally:
        stop_flag.set()
        api.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="XiaoZhi Desktop Corner Companion Widget")
    parser.add_argument("--console-url", default=DEFAULT_CONSOLE_URL)
    args = parser.parse_args(argv)
    return run_widget_app(console_url=args.console_url)


if __name__ == "__main__":
    raise SystemExit(main())
