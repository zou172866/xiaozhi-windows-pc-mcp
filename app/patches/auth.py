"""Config env expansion, local MCP Bearer injection, and ESP32 OTA/WS/Vision auth hardening (H-1, A-1, C-1).

Zero import-time side effects: call `install_config_env_expand_patch()`,
`install_mcp_token_injection_patch()`, and `install_esp32_auth_hardening_patch()` explicitly from `boot.install_all()`.
"""

from __future__ import annotations

import functools
import json
import os
import re
import secrets
import sys
from typing import Any

import patch_registry
import runtime_events
import turn_context_router
import xz_common

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_DEFAULTS = {
    "HOST_LAN_IP": "127.0.0.1",
    "HOST_DATA_DIR": xz_common.DATA_DIR,
    "HOST_VOICES_DIR": xz_common.VOICES_DIR,
    "HOST_GSV_WEIGHTS_DIR": os.path.join(xz_common.ROOT_DIR, "models", "gsv_weights"),
    "HOST_PROJECT_DIR": xz_common.ROOT_DIR,
}
_PATH_ENV_KEYS = frozenset({"HOST_DATA_DIR", "HOST_VOICES_DIR", "HOST_GSV_WEIGHTS_DIR", "HOST_PROJECT_DIR"})
_ENV_FALLBACKS = {
    "DOUBAO_ASR_APPID": ("DOUBAO_APPID",),
    "DOUBAO_ASR_ACCESS_TOKEN": ("DOUBAO_ACCESS_TOKEN",),
    "DOUBAO_TTS_APPID": ("DOUBAO_APPID",),
    "DOUBAO_TTS_ACCESS_TOKEN": ("DOUBAO_ACCESS_TOKEN",),
}
_EPHEMERAL_ESP32_AUTH_KEY = secrets.token_hex(32)


_MAC_DASH_RE = re.compile(r"^[0-9a-f]{2}(?:-[0-9a-f]{2}){5}$")


def _normalize_device_id(value) -> str:
    s = str(value or "").strip().lower()
    if _MAC_DASH_RE.match(s):
        return s.replace("-", ":")
    return s


def normalize_allowed_devices(raw_devices, env_val: str | None = None) -> list[str]:
    """Normalize ESP32 MAC / device-id allowlist from YAML + optional ESP32_ALLOWED_DEVICES env."""
    candidates: list[str] = []
    if isinstance(raw_devices, str):
        candidates.append(raw_devices)
    elif isinstance(raw_devices, (list, tuple, set)):
        for item in raw_devices:
            if item is not None:
                candidates.append(str(item))

    if env_val is None:
        env_val = os.environ.get("ESP32_ALLOWED_DEVICES", "")
    if env_val:
        candidates.append(str(env_val))

    out: list[str] = []
    seen: set[str] = set()
    for entry in candidates:
        for part in re.split(r"[,;\s]+", str(entry)):
            tok = _normalize_device_id(part)
            if not tok or "${" in tok or "}" in tok:
                continue
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def resolve_esp32_auth_key(raw_key: str | None) -> str:
    """Resolve ESP32 HMAC/JWT signing key without ever falling back to literal ${ESP32_AUTH_KEY}."""
    for cand in (raw_key, os.environ.get("ESP32_AUTH_KEY", "")):
        s = str(cand or "").strip()
        if s and len(s) >= 16 and "${" not in s and "你" not in s:
            return s
    return _EPHEMERAL_ESP32_AUTH_KEY


def _expand_env(obj):
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        host_dirs = xz_common.resolve_host_dirs()

        def _repl(m):
            key = m.group(1)
            if key in _PATH_ENV_KEYS:
                return host_dirs.get(key, _ENV_DEFAULTS.get(key, m.group(0)))
            val = os.environ.get(key)
            if val:
                return val
            for fb_key in _ENV_FALLBACKS.get(key, ()):
                fb_val = os.environ.get(fb_key)
                if fb_val:
                    return fb_val
            return _ENV_DEFAULTS.get(key, m.group(0))

        return _ENV_PATTERN.sub(_repl, obj)
    return obj


_AUTO_INTENT_FUNCTIONS: list[str] = []


def _inject_auto_intent_functions(data: Any) -> None:
    if not _AUTO_INTENT_FUNCTIONS or not isinstance(data, dict):
        return
    intent_cfg = data.get("Intent")
    if isinstance(intent_cfg, dict):
        fc_cfg = intent_cfg.get("function_call")
        if isinstance(fc_cfg, dict) and isinstance(fc_cfg.get("functions"), list):
            existing = set(fc_cfg["functions"])
            for fn_name in _AUTO_INTENT_FUNCTIONS:
                if fn_name not in existing:
                    fc_cfg["functions"].append(fn_name)
                    existing.add(fn_name)


def register_auto_intent_function(name: str) -> None:
    """Register a built-in tool name to auto-expose in Intent.function_call.functions."""
    fn = str(name or "").strip()
    if fn and fn not in _AUTO_INTENT_FUNCTIONS:
        _AUTO_INTENT_FUNCTIONS.append(fn)
    # Backfill already-cached main_config if earlier boot imports triggered check_config_file/load_config
    cache_mod = sys.modules.get("core.utils.cache.manager")
    if cache_mod is not None:
        try:
            cm = getattr(cache_mod, "cache_manager", None)
            ct = getattr(cache_mod, "CacheType", None)
            if cm is not None and ct is not None and hasattr(ct, "CONFIG"):
                cached_cfg = cm.get(ct.CONFIG, "main_config")
                if isinstance(cached_cfg, dict):
                    _inject_auto_intent_functions(cached_cfg)
        except Exception:
            pass


def _make_patched_read_config(_orig):
    @functools.wraps(_orig)
    def _patched_read_config(config_path, *args, **kwargs):
        data = _orig(config_path, *args, **kwargs)
        data = _expand_env(data)
        if isinstance(data, dict):
            if "turn_context_mode" in data:
                turn_context_router.set_config_turn_context_mode(data.get("turn_context_mode"))
            _inject_auto_intent_functions(data)
        if isinstance(data, dict) and isinstance(data.get("server"), dict):
            srv = data["server"]
            base_name = os.path.basename(str(config_path or ""))
            if base_name == "config.yaml":
                # Strip upstream's hardcoded example MAC ("11:22:33:44:55:66") so custom configs never inherit it.
                if isinstance(srv.get("auth"), dict):
                    srv["auth"]["allowed_devices"] = []
            else:
                if isinstance(srv.get("auth"), dict):
                    srv["auth"]["allowed_devices"] = normalize_allowed_devices(
                        srv["auth"].get("allowed_devices")
                    )
                srv["auth_key"] = resolve_esp32_auth_key(srv.get("auth_key"))
        return data

    return _patched_read_config



_patched_read_config = None


def install_config_env_expand_patch() -> Any:
    """Install in-memory ${ENV_VAR} expansion on config.config_loader.read_config."""
    global _patched_read_config
    import config.config_loader as config_loader

    patch_registry.install_patch(
        "config_env_expand",
        config_loader,
        "read_config",
        _make_patched_read_config,
        category="security",
        target="config.config_loader.read_config",
        required_params=("config_path",),
        require_async=False,
        rebind_modules=True,
        flag_obj=config_loader,
        flag_attr="_env_expand_patched",
    )
    _patched_read_config = config_loader.read_config
    return _patched_read_config


# 1b. Inject local 18900 Bearer Token into ServerMCPManager.load_config in memory (A-1 env PC_MCP_TOKEN first)
PC_MCP_TOKEN_PATH = xz_common.PC_MCP_TOKEN_PATH


def _read_pc_mcp_token(token_path: str | None = None) -> str:
    return xz_common.get_pc_mcp_token(token_path if token_path is not None else PC_MCP_TOKEN_PATH)


def _is_local_pc_mcp_url(url: str) -> bool:
    if not url:
        return False
    try:
        from urllib.parse import urlparse

        p = urlparse(str(url).strip())
        host = (p.hostname or "").lower()
        port = p.port
        return host in ("host.docker.internal", "127.0.0.1", "localhost") and port == 18900
    except Exception:
        return False


def inject_pc_mcp_auth_headers(servers_cfg: dict, token_path: str | None = None) -> dict:
    if not isinstance(servers_cfg, dict):
        return servers_cfg
    token = _read_pc_mcp_token(token_path)
    if not token:
        return servers_cfg
    out = {}
    for name, cfg in servers_cfg.items():
        if isinstance(cfg, dict) and _is_local_pc_mcp_url(str(cfg.get("url") or "")):
            new_cfg = dict(cfg)
            headers = dict(new_cfg.get("headers") or {})
            headers["Authorization"] = f"Bearer {token}"
            new_cfg["headers"] = headers
            out[name] = new_cfg
        else:
            out[name] = cfg
    return out


def install_mcp_token_injection_patch() -> None:
    try:
        from core.providers.tools.server_mcp import mcp_manager as _mcp_mgr_mod

        cls = _mcp_mgr_mod.ServerMCPManager

        def _wrap_load_config(_orig_load_config):
            @functools.wraps(_orig_load_config)
            def _patched_load_config(self, *args, **kwargs):
                servers = _orig_load_config(self, *args, **kwargs)
                return inject_pc_mcp_auth_headers(servers)

            return _patched_load_config

        patch_registry.install_patch(
            "mcp_token_injection",
            cls,
            "load_config",
            _wrap_load_config,
            category="security",
            target="ServerMCPManager.load_config",
            required_params=("self",),
            require_async=False,
            flag_obj=cls,
            flag_attr="_pc_mcp_token_patched",
        )
    except Exception as _mcp_exc:
        patch_registry.record_patch(
            "mcp_token_injection",
            "failed",
            target="ServerMCPManager.load_config",
            detail=str(_mcp_exc),
            contract_ok=False,
            category="security",
        )
        print(f"[MCPAuth] 注入本地 18900 Bearer Token 补丁失败: {_mcp_exc}", flush=True)



# 1c. Harden ESP32 18000 WebSocket, 18003 OTA, and 18003 vision_explain authentication (P0 #1)
def _has_unexpanded_or_invalid_auth_key(handler) -> bool:
    """Return True if the handler's signing secret is missing, empty, or contains an unexpanded ${...} placeholder (P0-1)."""
    auth_obj = getattr(handler, "auth", None)
    if auth_obj is None:
        return True
    candidates = []
    if hasattr(auth_obj, "secret_key"):
        candidates.append(getattr(auth_obj, "secret_key", None))
    srv_cfg = (getattr(handler, "config", None) or {}).get("server") or {}
    if isinstance(srv_cfg, dict) and "auth_key" in srv_cfg:
        candidates.append(srv_cfg.get("auth_key"))
    if not candidates:
        return True
    for raw in candidates:
        if isinstance(raw, (bytes, bytearray)):
            s = raw.decode("utf-8", errors="replace").strip()
        else:
            s = str(raw or "").strip()
        if not s or "${" in s or "你" in s:
            return True
    return False


def _extract_handler_allowed_devices(handler) -> set[str]:
    live_synced = runtime_events.get_live_allowed_devices_for_handler(handler)
    if live_synced is None:
        live_synced = runtime_events.get_live_allowed_devices_for_config(getattr(handler, "config", None))
    if live_synced is not None:
        return set(live_synced)
    if hasattr(handler, "allowed_devices"):
        raw = getattr(handler, "allowed_devices")
    else:
        raw = (
            ((getattr(handler, "config", None) or {}).get("server") or {})
            .get("auth", {})
            .get("allowed_devices")
        )
    return set(normalize_allowed_devices(raw, env_val=""))


def install_esp32_auth_hardening_patch() -> None:
    """Enforce fail-closed MAC allowlist + Bearer token auth on 18000 WS, 18003 OTA, and 18003 vision_explain."""
    # 1c-1. OTAHandler (18003 /xiaozhi/ota/): reject non-allowlisted MACs with 403 and sign tokens ONLY for allowlisted MACs
    try:
        from aiohttp import web as _aiohttp_web
        from core.api import ota_handler as _ota_mod

        ota_cls = _ota_mod.OTAHandler
        if getattr(ota_cls, "_esp32_auth_hardened", False):
            if patch_registry.get_patch_status("esp32_auth_ota") is None:
                patch_registry.record_patch(
                    "esp32_auth_ota",
                    "ok",
                    target="OTAHandler.handle_post",
                    detail="already applied",
                    contract_ok=True,
                    category="security",
                )
        else:
            _orig_ota_handle_post = ota_cls.handle_post
            ok_ota, det_ota = patch_registry.verify_signature_contract(
                _orig_ota_handle_post, required_params=("self", "request"), require_async=True
            )
            if not ok_ota:
                patch_registry.record_patch(
                    "esp32_auth_ota",
                    "skipped_contract",
                    target="OTAHandler.handle_post",
                    detail=det_ota,
                    contract_ok=False,
                    category="security",
                )
                print(f"[ESP32Auth] OTAHandler 签名契约不匹配，拒绝挂载: {det_ota}", flush=True)
            else:
                @functools.wraps(_orig_ota_handle_post)
                async def _patched_ota_handle_post(self, request, *args, **kwargs):
                    runtime_events.register_auth_handler(self)
                    auth_enabled = bool(getattr(self, "auth_enable", False))
                    hdrs = {str(k).lower(): str(v) for k, v in dict(request.headers).items()}
                    dev_raw = (hdrs.get("device-id") or "").strip()
                    client_raw = (hdrs.get("client-id") or "").strip()
                    dev_norm = _normalize_device_id(dev_raw)
                    if auth_enabled:
                        if _has_unexpanded_or_invalid_auth_key(self):
                            try:
                                self.logger.bind(tag="esp32_auth").error(
                                    "服务端 auth_key 无效或包含未展开占位符，拒绝签发 OTA 令牌"
                                )
                            except Exception:
                                pass
                            resp = _aiohttp_web.Response(
                                text=json.dumps(
                                    {"success": False, "message": "服务端认证密钥未正确展开，拒绝签发令牌"},
                                    ensure_ascii=False,
                                ),
                                content_type="application/json",
                                status=503,
                            )
                            self._add_cors_headers(resp)
                            return resp
                        if not dev_norm or not client_raw:
                            resp = _aiohttp_web.Response(
                                text=json.dumps(
                                    {"success": False, "message": "OTA请求缺少device-id或client-id"},
                                    ensure_ascii=False,
                                ),
                                content_type="application/json",
                                status=400,
                            )
                            self._add_cors_headers(resp)
                            return resp
                        allowed = _extract_handler_allowed_devices(self)
                        if not allowed or dev_norm not in allowed:
                            if dev_norm:
                                try:
                                    runtime_events.publish_event(
                                        "device_rejected",
                                        device_id=dev_norm,
                                        source="ota",
                                        client_id=client_raw,
                                        reason="not_in_allowlist",
                                    )
                                except Exception:
                                    pass
                            try:
                                self.logger.bind(tag="esp32_auth").warning(
                                    f"拒绝未授权设备 OTA 请求: device_id={dev_norm} "
                                    "(请打开 http://localhost:18080 顶部白名单卡片点击「一键放行」，或在 .env 配置 ESP32_ALLOWED_DEVICES)"
                                )
                            except Exception:
                                pass
                            resp = _aiohttp_web.Response(
                                text=json.dumps(
                                    {
                                        "success": False,
                                        "error": "device_not_allowlisted",
                                        "device_id": dev_norm,
                                        "message": f"设备未在白名单中 ({dev_norm})，拒绝下发配置；请打开 http://localhost:18080 点击「一键放行」",
                                    },
                                    ensure_ascii=False,
                                ),
                                content_type="application/json",
                                status=403,
                            )
                            self._add_cors_headers(resp)
                            return resp

                    if dev_raw:
                        try:
                            runtime_events.publish_event("device_seen", device_id=dev_raw)
                        except Exception:
                            pass

                    resp = await _orig_ota_handle_post(self, request, *args, **kwargs)
                    if auth_enabled and getattr(resp, "status", 0) == 200:
                        try:
                            body_txt = resp.text if isinstance(getattr(resp, "text", None), str) else ""
                            data_obj = json.loads(body_txt) if body_txt else None
                            if isinstance(data_obj, dict) and isinstance(data_obj.get("websocket"), dict):
                                data_obj["websocket"]["token"] = self.auth.generate_token(
                                    client_raw, dev_norm
                                )
                                new_resp = _aiohttp_web.Response(
                                    text=json.dumps(data_obj, separators=(",", ":")),
                                    content_type="application/json",
                                    status=200,
                                )
                                self._add_cors_headers(new_resp)
                                return new_resp
                        except Exception as exc:
                            try:
                                self.logger.bind(tag="esp32_auth").error(f"OTA 注入签名令牌失败: {exc}")
                            except Exception:
                                pass
                            err_resp = _aiohttp_web.Response(
                                text=json.dumps(
                                    {"success": False, "message": "OTA 签名令牌生成失败，拒绝签发未签名响应"},
                                    ensure_ascii=False,
                                ),
                                content_type="application/json",
                                status=503,
                            )
                            self._add_cors_headers(err_resp)
                            return err_resp
                    return resp

                ota_cls.handle_post = _patched_ota_handle_post
                _orig_ota_init = getattr(ota_cls, "__init__", None)
                if callable(_orig_ota_init) and not getattr(_orig_ota_init, "_esp32_auth_init_wrapped", False):
                    @functools.wraps(_orig_ota_init)
                    def _patched_ota_init(self, *args, **kwargs):
                        res = _orig_ota_init(self, *args, **kwargs)
                        runtime_events.register_auth_handler(self)
                        return res

                    _patched_ota_init._esp32_auth_init_wrapped = True
                    ota_cls.__init__ = _patched_ota_init
                ota_cls._esp32_auth_hardened = True
                patch_registry.record_patch(
                    "esp32_auth_ota",
                    "ok",
                    target="OTAHandler.handle_post",
                    detail=det_ota,
                    contract_ok=True,
                    category="security",
                )
    except Exception as _ota_exc:
        patch_registry.record_patch(
            "esp32_auth_ota",
            "failed",
            target="OTAHandler.handle_post",
            detail=str(_ota_exc),
            contract_ok=False,
            category="security",
        )
        print(f"[ESP32Auth] OTAHandler 加固补丁失败: {_ota_exc}", flush=True)

    # 1c-2. WebSocketServer (18000 /xiaozhi/v1/): require BOTH allowlisted MAC AND valid HMAC Bearer token
    #       + register live WebSocketServer / ConnectionHandler for P1-11 ready event & P1-13 hot config reload
    try:
        from core import websocket_server as _ws_mod
        from core.auth import AuthenticationError as _AuthErr
        from core.connection import ConnectionHandler as _ConnCls

        ws_cls = _ws_mod.WebSocketServer
        if getattr(ws_cls, "_esp32_auth_hardened", False):
            if patch_registry.get_patch_status("esp32_auth_ws") is None:
                patch_registry.record_patch(
                    "esp32_auth_ws",
                    "ok",
                    target="WebSocketServer._handle_auth/__init__/ConnectionHandler.close",
                    detail="already applied",
                    contract_ok=True,
                    category="security",
                )
        else:
            _orig_ws_handle_auth = ws_cls._handle_auth
            _orig_ws_init = ws_cls.__init__
            ok_ws, det_ws = patch_registry.verify_signature_contract(
                _orig_ws_handle_auth, required_params=("self", "websocket"), require_async=True
            )
            ok_ws_init, det_ws_init = patch_registry.verify_signature_contract(
                _orig_ws_init, required_params=("self", "config"), require_async=False
            )
            ok_conn_init, det_conn_init = True, "already_tracked"
            ok_conn_close, det_conn_close = True, "already_tracked"
            need_track_conn = not getattr(_ConnCls, "_runtime_tracked", False)
            if need_track_conn:
                _orig_conn_init = _ConnCls.__init__
                _orig_conn_close = _ConnCls.close
                ok_conn_init, det_conn_init = patch_registry.verify_signature_contract(
                    _orig_conn_init, required_params=("self", "config"), require_async=False
                )
                ok_conn_close, det_conn_close = patch_registry.verify_signature_contract(
                    _orig_conn_close, required_params=("self",), require_async=True
                )

            all_ws_ok = ok_ws and ok_ws_init and ok_conn_init and ok_conn_close
            if not all_ws_ok:
                patch_registry.record_patch(
                    "esp32_auth_ws",
                    "skipped_contract",
                    target="WebSocketServer._handle_auth/__init__/ConnectionHandler.close",
                    detail=f"{det_ws}; init={det_ws_init}; conn={det_conn_init}; close={det_conn_close}",
                    contract_ok=False,
                    category="security",
                )
                print(
                    f"[ESP32Auth] WebSocketServer 签名契约不匹配，拒绝挂载: {det_ws}; init={det_ws_init}",
                    flush=True,
                )
            else:
                @functools.wraps(_orig_ws_handle_auth)
                async def _patched_ws_handle_auth(self, websocket, *args, **kwargs):
                    runtime_events.register_auth_handler(self)
                    raw_headers = dict(getattr(getattr(websocket, "request", None), "headers", None) or {})
                    headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
                    dev_raw = (headers.get("device-id") or "").strip()
                    if not bool(getattr(self, "auth_enable", False)):
                        if dev_raw:
                            try:
                                runtime_events.publish_event("device_seen", device_id=dev_raw)
                            except Exception:
                                pass
                        return
                    dev_norm = _normalize_device_id(dev_raw)
                    client_id = (headers.get("client-id") or "").strip()
                    auth_hdr = (headers.get("authorization") or "").strip()

                    def _reject(reason: str):
                        try:
                            lg = getattr(self, "logger", None)
                            if lg is not None:
                                lg.bind(tag="esp32_auth").warning(
                                    f"拒绝 WebSocket 连接: device_id={dev_norm or 'missing'}, "
                                    f"client_id={client_id or 'missing'}, "
                                    f"has_bearer={auth_hdr.lower().startswith('bearer ')}, reason={reason}"
                                )
                        except Exception:
                            pass
                        raise _AuthErr(reason)

                    if _has_unexpanded_or_invalid_auth_key(self):
                        _reject("Server auth_key is empty or contains unexpanded placeholder")

                    allowed = _extract_handler_allowed_devices(self)
                    if not dev_norm or not allowed or dev_norm not in allowed:
                        if dev_norm:
                            try:
                                runtime_events.publish_event(
                                    "device_rejected",
                                    device_id=dev_norm,
                                    source="ws",
                                    client_id=client_id,
                                    reason="not_in_allowlist",
                                )
                            except Exception:
                                pass
                        _reject(f"Device not in allowlist: {dev_norm or 'missing'}")

                    if not auth_hdr.lower().startswith("bearer "):
                        _reject("Missing or invalid Authorization header")
                    token = auth_hdr[7:].strip()
                    if not token or not client_id:
                        _reject("Missing token or client-id")

                    auth_ok = self.auth.verify_token(token, client_id=client_id, username=dev_norm)
                    if not auth_ok and dev_raw != dev_norm:
                        auth_ok = self.auth.verify_token(token, client_id=client_id, username=dev_raw)
                    if not auth_ok:
                        _reject("Invalid or expired token")

                    if dev_raw:
                        try:
                            runtime_events.publish_event("device_seen", device_id=dev_raw)
                        except Exception:
                            pass

                @functools.wraps(_orig_ws_init)
                def _patched_ws_init(self, *args, **kwargs):
                    res = _orig_ws_init(self, *args, **kwargs)
                    runtime_events.register_ws_server(self)
                    runtime_events.publish_event("server_ready")
                    return res

                ws_cls._handle_auth = _patched_ws_handle_auth
                ws_cls.__init__ = _patched_ws_init
                ws_cls._esp32_auth_hardened = True

                if need_track_conn:
                    @functools.wraps(_orig_conn_init)
                    def _tracked_conn_init(self, *args, **kwargs):
                        res = _orig_conn_init(self, *args, **kwargs)
                        runtime_events.register_connection(self)
                        return res

                    @functools.wraps(_orig_conn_close)
                    async def _tracked_conn_close(self, *args, **kwargs):
                        try:
                            return await _orig_conn_close(self, *args, **kwargs)
                        finally:
                            runtime_events.unregister_connection(self)
                            try:
                                runtime_events.publish_event(
                                    "connection_closed",
                                    device_id=str(getattr(self, "device_id", "") or ""),
                                )
                            except Exception:
                                pass

                    _ConnCls.__init__ = _tracked_conn_init
                    _ConnCls.close = _tracked_conn_close
                    _ConnCls._runtime_tracked = True

                patch_registry.record_patch(
                    "esp32_auth_ws",
                    "ok",
                    target="WebSocketServer._handle_auth/__init__/ConnectionHandler.close",
                    detail=f"{det_ws}; init={det_ws_init}; conn={det_conn_init}; close={det_conn_close}",
                    contract_ok=True,
                    category="security",
                )
    except Exception as _ws_exc:
        patch_registry.record_patch(
            "esp32_auth_ws",
            "failed",
            target="WebSocketServer._handle_auth",
            detail=str(_ws_exc),
            contract_ok=False,
            category="security",
        )
        print(f"[ESP32Auth] WebSocketServer 加固补丁失败: {_ws_exc}", flush=True)

    # 1c-3. VisionHandler (18003 /mcp/vision/explain): remove `web_test_client` backdoor + enforce JWT & MAC allowlist
    try:
        from aiohttp import web as _aiohttp_web
        from core.api import vision_handler as _vis_mod

        vis_cls = _vis_mod.VisionHandler
        if getattr(vis_cls, "_esp32_auth_hardened", False):
            if patch_registry.get_patch_status("esp32_auth_vision") is None:
                patch_registry.record_patch(
                    "esp32_auth_vision",
                    "ok",
                    target="VisionHandler._verify_auth_token/handle_post",
                    detail="already applied",
                    contract_ok=True,
                    category="security",
                )
        else:
            _orig_vision_verify = vis_cls._verify_auth_token
            _orig_vision_handle_post = vis_cls.handle_post
            ok_v1, d_v1 = patch_registry.verify_signature_contract(
                _orig_vision_verify, required_params=("self", "request"), require_async=False
            )
            ok_v2, d_v2 = patch_registry.verify_signature_contract(
                _orig_vision_handle_post, required_params=("self", "request"), require_async=True
            )
            if not (ok_v1 and ok_v2):
                patch_registry.record_patch(
                    "esp32_auth_vision",
                    "skipped_contract",
                    target="VisionHandler._verify_auth_token/handle_post",
                    detail=f"{d_v1}; {d_v2}",
                    contract_ok=False,
                    category="security",
                )
                print(
                    f"[ESP32Auth] VisionHandler 签名契约不匹配，拒绝挂载: {d_v1}; {d_v2}",
                    flush=True,
                )
            else:
                @functools.wraps(_orig_vision_verify)
                def _patched_vision_verify_auth_token(self, request, *args, **kwargs):
                    if _has_unexpanded_or_invalid_auth_key(self):
                        return False, None
                    hdrs = {str(k).lower(): str(v) for k, v in dict(request.headers).items()}
                    auth_header = (hdrs.get("authorization") or "").strip()
                    if not auth_header.lower().startswith("bearer "):
                        return False, None
                    token = auth_header[7:].strip()
                    if not token:
                        return False, None
                    is_valid, token_device_id = self.auth.verify_token(token)
                    if not is_valid or not token_device_id:
                        return False, None
                    return True, str(token_device_id).strip()

                @functools.wraps(_orig_vision_handle_post)
                async def _patched_vision_handle_post(self, request, *args, **kwargs):
                    runtime_events.register_auth_handler(self)
                    is_valid, token_device_id = self._verify_auth_token(request)
                    if not is_valid:
                        resp = _aiohttp_web.Response(
                            text=json.dumps(
                                self._create_error_response("无效的认证token或token已过期"),
                                ensure_ascii=False,
                            ),
                            content_type="application/json",
                            status=401,
                        )
                        self._add_cors_headers(resp)
                        return resp

                    hdrs = {str(k).lower(): str(v) for k, v in dict(request.headers).items()}
                    device_id_raw = (hdrs.get("device-id") or "").strip()
                    dev_norm = _normalize_device_id(device_id_raw)
                    tok_norm = _normalize_device_id(token_device_id)
                    if not dev_norm or dev_norm != tok_norm:
                        resp = _aiohttp_web.Response(
                            text=json.dumps(
                                self._create_error_response("设备ID与token不匹配"),
                                ensure_ascii=False,
                            ),
                            content_type="application/json",
                            status=403,
                        )
                        self._add_cors_headers(resp)
                        return resp

                    srv_cfg = (getattr(self, "config", None) or {}).get("server") or {}
                    auth_cfg = srv_cfg.get("auth") or {}
                    if bool(auth_cfg.get("enabled", False)):
                        allowed = _extract_handler_allowed_devices(self)
                        if not allowed or dev_norm not in allowed:
                            if dev_norm:
                                try:
                                    runtime_events.publish_event(
                                        "device_rejected",
                                        device_id=dev_norm,
                                        source="vision",
                                        reason="not_in_allowlist",
                                    )
                                except Exception:
                                    pass
                            resp = _aiohttp_web.Response(
                                text=json.dumps(
                                    self._create_error_response("设备未在白名单中，拒绝访问视觉分析接口"),
                                    ensure_ascii=False,
                                ),
                                content_type="application/json",
                                status=403,
                            )
                            self._add_cors_headers(resp)
                            return resp

                    return await _orig_vision_handle_post(self, request, *args, **kwargs)

                vis_cls._verify_auth_token = _patched_vision_verify_auth_token
                vis_cls.handle_post = _patched_vision_handle_post
                _orig_vis_init = getattr(vis_cls, "__init__", None)
                if callable(_orig_vis_init) and not getattr(_orig_vis_init, "_esp32_auth_init_wrapped", False):
                    @functools.wraps(_orig_vis_init)
                    def _patched_vis_init(self, *args, **kwargs):
                        res = _orig_vis_init(self, *args, **kwargs)
                        runtime_events.register_auth_handler(self)
                        return res

                    _patched_vis_init._esp32_auth_init_wrapped = True
                    vis_cls.__init__ = _patched_vis_init
                vis_cls._esp32_auth_hardened = True
                patch_registry.record_patch(
                    "esp32_auth_vision",
                    "ok",
                    target="VisionHandler._verify_auth_token/handle_post",
                    detail=f"{d_v1}; {d_v2}",
                    contract_ok=True,
                    category="security",
                )
    except Exception as _vis_exc:
        patch_registry.record_patch(
            "esp32_auth_vision",
            "failed",
            target="VisionHandler.handle_post",
            detail=str(_vis_exc),
            contract_ok=False,
            category="security",
        )
        print(f"[ESP32Auth] VisionHandler 加固补丁失败: {_vis_exc}", flush=True)

