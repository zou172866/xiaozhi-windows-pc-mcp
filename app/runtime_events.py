"""In-process structured runtime event ring buffer & live server/connection registry.

Replaces fragile Chinese log-regex parsing in console_server.py (P1-11) and enables
zero-restart hot config reload (P1-13) + live speaking busy checks for TTS preview (P1-14).
"""

from __future__ import annotations

import re
import threading
import time
import weakref
from collections import OrderedDict, deque
from typing import Any, Callable, Dict, Iterable, List, Optional

RING_BUFFER_MAXLEN = 512
REJECTED_DEVICES_MAX = 20
_MAC_DASH_RE = re.compile(r"^[0-9a-f]{2}(?:-[0-9a-f]{2}){5}$")

_lock = threading.RLock()
_events: deque[Dict[str, Any]] = deque(maxlen=RING_BUFFER_MAXLEN)
_seq: int = 0
_subscribers: List[Callable[[Dict[str, Any]], None]] = []
_hooks_active: bool = False

_runtime_lock = threading.RLock()
_ws_servers: "weakref.WeakSet[Any]" = weakref.WeakSet()
_auth_handlers: "weakref.WeakSet[Any]" = weakref.WeakSet()
_active_connections: "weakref.WeakSet[Any]" = weakref.WeakSet()
_rejected_devices: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_live_allowed_devices_by_handler: "weakref.WeakKeyDictionary[Any, set[str]]" = weakref.WeakKeyDictionary()
_latest_live_allowed_devices: Optional[set[str]] = None
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
                _rejected_devices.pop(dev.lower(), None)
        elif event_type == "device_rejected":
            dev = str(fields.get("device_id") or "").strip().lower()[:64]
            if dev:
                prev = _rejected_devices.get(dev)
                count = int(prev.get("count", 0)) + 1 if isinstance(prev, dict) else 1
                _rejected_devices[dev] = {
                    "device_id": dev,
                    "source": str(fields.get("source") or "unknown").strip()[:16],
                    "client_id": str(fields.get("client_id") or "").strip()[:64],
                    "reason": str(fields.get("reason") or "not_in_allowlist").strip()[:64],
                    "last_seen": ts_short,
                    "last_ts": now,
                    "count": count,
                }
                _rejected_devices.move_to_end(dev)
                while len(_rejected_devices) > REJECTED_DEVICES_MAX:
                    _rejected_devices.popitem(last=False)
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


def _norm_device_token(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if _MAC_DASH_RE.match(s):
        return s.replace("-", ":")
    return s


def _apply_live_allowed_devices_locked(target: Any, normalized: set[str]) -> None:
    cfg = getattr(target, "config", None)
    if isinstance(cfg, dict):
        srv = cfg.get("server")
        if not isinstance(srv, dict):
            srv = {}
            cfg["server"] = srv
        auth_blk = srv.get("auth")
        if not isinstance(auth_blk, dict):
            auth_blk = {}
            srv["auth"] = auth_blk
        auth_blk["allowed_devices"] = sorted(normalized)
    if hasattr(target, "allowed_devices"):
        try:
            target.allowed_devices = set(normalized)
        except Exception:
            pass
    try:
        _live_allowed_devices_by_handler[target] = set(normalized)
    except TypeError:
        pass


def _is_target_synced_locked(target: Any) -> bool:
    try:
        return target in _live_allowed_devices_by_handler
    except TypeError:
        return False


def register_ws_server(server: Any) -> None:
    if server is None:
        return
    with _runtime_lock:
        try:
            _ws_servers.add(server)
        except TypeError:
            pass
        if _latest_live_allowed_devices is not None and not _is_target_synced_locked(server):
            _apply_live_allowed_devices_locked(server, _latest_live_allowed_devices)


def get_ws_servers() -> List[Any]:
    with _runtime_lock:
        return list(_ws_servers)


def register_auth_handler(handler: Any) -> None:
    if handler is None:
        return
    with _runtime_lock:
        try:
            _auth_handlers.add(handler)
        except TypeError:
            pass
        if _latest_live_allowed_devices is not None and not _is_target_synced_locked(handler):
            _apply_live_allowed_devices_locked(handler, _latest_live_allowed_devices)


def get_auth_handlers() -> List[Any]:
    with _runtime_lock:
        return list(_auth_handlers)


def get_rejected_devices() -> List[Dict[str, Any]]:
    """Return recently rejected/unauthorized ESP32 devices (newest first)."""
    with _runtime_lock:
        return [dict(v) for v in reversed(_rejected_devices.values())]


def clear_rejected_device(device_id: str) -> bool:
    dev = _norm_device_token(device_id)
    if not dev:
        return False
    with _runtime_lock:
        return _rejected_devices.pop(dev, None) is not None


def set_live_allowed_devices(allowed_devices: Iterable[str]) -> None:
    """Sync runtime ESP32 MAC allowlist to live registered servers/handlers without affecting isolated unit mocks."""
    global _latest_live_allowed_devices
    normalized = {tok for d in allowed_devices if (tok := _norm_device_token(d))}
    with _runtime_lock:
        _latest_live_allowed_devices = set(normalized)
        _live_allowed_devices_by_handler.clear()
        for srv in list(_ws_servers):
            _apply_live_allowed_devices_locked(srv, normalized)
        for handler in list(_auth_handlers):
            _apply_live_allowed_devices_locked(handler, normalized)
        for dev in normalized:
            _rejected_devices.pop(dev, None)


def get_live_allowed_devices_for_handler(handler: Any) -> Optional[set[str]]:
    """Return synced live allowlist if `handler` is a registered live server/handler instance."""
    if handler is None:
        return None
    with _runtime_lock:
        try:
            live_set = _live_allowed_devices_by_handler.get(handler)
        except TypeError:
            live_set = None
        return set(live_set) if live_set is not None else None


def get_live_allowed_devices_for_config(config_obj: Any) -> Optional[set[str]]:
    """Return synced live allowlist if `config_obj` belongs to a live registered server/handler."""
    if not isinstance(config_obj, dict):
        return None
    with _runtime_lock:
        if _latest_live_allowed_devices is None:
            return None
        for target in (*list(_ws_servers), *list(_auth_handlers)):
            if getattr(target, "config", None) is config_obj:
                try:
                    live_set = _live_allowed_devices_by_handler.get(target)
                except TypeError:
                    live_set = None
                if live_set is not None:
                    return set(live_set)
        return None


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
    global _seq, _hooks_active, _latest_activity, _latest_device_id, _server_ready, _latest_live_allowed_devices
    with _lock:
        _events.clear()
        _seq = 0
        _hooks_active = False
    with _runtime_lock:
        _ws_servers.clear()
        _auth_handlers.clear()
        _active_connections.clear()
        _rejected_devices.clear()
        _live_allowed_devices_by_handler.clear()
        _latest_live_allowed_devices = None
        _latest_activity = "idle"
        _latest_device_id = ""
        _server_ready = False
