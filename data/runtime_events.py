"""In-process structured runtime event ring buffer & live server/connection registry.

Replaces fragile Chinese log-regex parsing in console_server.py (P1-11) and enables
zero-restart hot config reload (P1-13) + live speaking busy checks for TTS preview (P1-14).
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import deque
from typing import Any, Callable, Dict, List

RING_BUFFER_MAXLEN = 512

_lock = threading.RLock()
_events: deque[Dict[str, Any]] = deque(maxlen=RING_BUFFER_MAXLEN)
_seq: int = 0
_subscribers: List[Callable[[Dict[str, Any]], None]] = []
_hooks_active: bool = False

_runtime_lock = threading.RLock()
_ws_servers: "weakref.WeakSet[Any]" = weakref.WeakSet()
_active_connections: "weakref.WeakSet[Any]" = weakref.WeakSet()
_latest_activity: str = "idle"
_latest_device_id: str = ""
_server_ready: bool = False


def mark_hooks_active(active: bool = True) -> None:
    global _hooks_active
    with _lock:
        _hooks_active = bool(active)


def is_hooks_active() -> bool:
    with _lock:
        return _hooks_active


def publish_event(event_type: str, **fields: Any) -> Dict[str, Any]:
    """Push a structured runtime event into the in-process ring buffer and notify subscribers."""
    global _seq, _latest_activity, _latest_device_id, _server_ready
    now = time.time()
    ts_short = str(fields.pop("time", "") or time.strftime("%H:%M:%S", time.localtime(now)))
    with _lock:
        _seq += 1
        evt: Dict[str, Any] = {
            "seq": _seq,
            "ts": now,
            "time": ts_short,
            "type": str(event_type),
        }
        evt.update(fields)
        _events.append(evt)
        subs = list(_subscribers)

    with _runtime_lock:
        if event_type == "server_ready":
            _server_ready = True
        elif event_type == "device_seen":
            dev = str(fields.get("device_id") or "").strip()
            if dev:
                _latest_device_id = dev
        elif event_type == "listen_start":
            _latest_activity = "listening"
        elif event_type == "user_message":
            _latest_activity = "speaking"
        elif event_type == "tts_state":
            st = str(fields.get("state") or "")
            if st in ("start", "sentence_start"):
                _latest_activity = "speaking"
            elif st == "stop":
                ev_dev = str(fields.get("device_id") or "").strip()
                other_speaking = any(
                    _conn_is_speaking(c)
                    and (not ev_dev or str(getattr(c, "device_id", "") or "").strip() != ev_dev)
                    for c in list(_active_connections)
                )
                if not ev_dev or not other_speaking:
                    _latest_activity = "idle"
        elif event_type == "connection_closed":
            if not any(_conn_is_speaking(c) for c in list(_active_connections)):
                _latest_activity = "idle"

    for cb in subs:
        try:
            cb(dict(evt))
        except Exception:
            pass
    return dict(evt)


def subscribe(callback: Callable[[Dict[str, Any]], None]) -> None:
    with _lock:
        if callback not in _subscribers:
            _subscribers.append(callback)


def unsubscribe(callback: Callable[[Dict[str, Any]], None]) -> None:
    with _lock:
        if callback in _subscribers:
            _subscribers.remove(callback)


def get_events_since(after_seq: int = 0) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(e) for e in _events if int(e.get("seq", 0)) > int(after_seq)]


def get_recent_events(limit: int = 100) -> List[Dict[str, Any]]:
    n = max(1, int(limit))
    with _lock:
        items = list(_events)[-n:]
        return [dict(e) for e in items]


def register_ws_server(server: Any) -> None:
    if server is None:
        return
    with _runtime_lock:
        try:
            _ws_servers.add(server)
        except TypeError:
            pass


def get_ws_servers() -> List[Any]:
    with _runtime_lock:
        return list(_ws_servers)


def register_connection(conn: Any) -> None:
    if conn is None:
        return
    with _runtime_lock:
        try:
            _active_connections.add(conn)
        except TypeError:
            pass


def _conn_is_speaking(conn: Any) -> bool:
    try:
        if getattr(conn, "client_is_speaking", False):
            return True
        if getattr(conn, "_pc_audio_active_sid", None) is not None:
            return True
    except Exception:
        pass
    return False


def unregister_connection(conn: Any) -> None:
    global _latest_activity
    if conn is None:
        return
    with _runtime_lock:
        try:
            _active_connections.discard(conn)
        except TypeError:
            pass
        if _latest_activity == "speaking" and not any(_conn_is_speaking(c) for c in list(_active_connections)):
            _latest_activity = "idle"


def get_active_connections() -> List[Any]:
    with _runtime_lock:
        return list(_active_connections)


def is_any_device_speaking() -> bool:
    """Return True if any live ESP32 connection is speaking/synthesizing or latest state is speaking."""
    with _runtime_lock:
        conns = list(_active_connections)
        act = _latest_activity
    for conn in conns:
        if _conn_is_speaking(conn):
            return True
    return act == "speaking"


def get_latest_runtime_snapshot() -> Dict[str, Any]:
    with _runtime_lock:
        return {
            "server_ready": _server_ready,
            "device_id": _latest_device_id,
            "activity": _latest_activity,
            "active_connections": len(_active_connections),
        }


def reset_runtime_events() -> None:
    """Clear ring buffer and runtime tracking (for isolated unit tests)."""
    global _seq, _hooks_active, _latest_activity, _latest_device_id, _server_ready
    with _lock:
        _events.clear()
        _seq = 0
        _hooks_active = False
    with _runtime_lock:
        _ws_servers.clear()
        _active_connections.clear()
        _latest_activity = "idle"
        _latest_device_id = ""
        _server_ready = False
