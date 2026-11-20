import functools
import json
import os
import re
import secrets
import sys
import runpy
import threading
import time

ROOT_DIR = "/opt/xiaozhi-esp32-server"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
for p in (ROOT_DIR, APP_DIR, DATA_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(ROOT_DIR)

import patch_registry
import runtime_events
import runtime_settings
import turn_context_router
import xz_common

# 0. Verify upstream SERVER_VERSION contract & migrate legacy runtime JSONs at startup (P1-8, P2-21)
_ver_info = patch_registry.check_upstream_server_version()
if not _ver_info.get("match"):
    print(f"[PatchContract] WARNING: {_ver_info.get('detail')}", flush=True)
try:
    runtime_settings.migrate_legacy_runtime_settings()
except Exception as _rs_exc:
    print(f"[RuntimeSettings] 启动迁移跳过: {_rs_exc}", flush=True)

# 1. Expand ${ENV_VAR} in memory so API keys never sit in plaintext on disk
import config.config_loader as config_loader

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_DEFAULTS = {
    "HOST_LAN_IP": "127.0.0.1",
    "HOST_DATA_DIR": "/opt/xiaozhi-esp32-server/data",
    "HOST_VOICES_DIR": "/opt/xiaozhi-esp32-server/voices",
    "HOST_GSV_WEIGHTS_DIR": "/opt/xiaozhi-esp32-server/models/gsv_weights",
    "HOST_PROJECT_DIR": "/opt/xiaozhi-esp32-server",
}
_PATH_ENV_KEYS = frozenset({"HOST_DATA_DIR", "HOST_VOICES_DIR", "HOST_GSV_WEIGHTS_DIR", "HOST_PROJECT_DIR"})
_EPHEMERAL_ESP32_AUTH_KEY = secrets.token_hex(32)


def _normalize_device_id(value) -> str:
    return str(value or "").strip().lower()


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
            tok = part.strip().lower()
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
            return _ENV_DEFAULTS.get(key, m.group(0))

        return _ENV_PATTERN.sub(_repl, obj)
    return obj


def _make_patched_read_config(_orig):
    @functools.wraps(_orig)
    def _patched_read_config(config_path, *args, **kwargs):
        data = _orig(config_path, *args, **kwargs)
        data = _expand_env(data)
        if isinstance(data, dict):
            if "turn_context_mode" in data:
                turn_context_router.set_config_turn_context_mode(data.get("turn_context_mode"))
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


patch_registry.install_patch(
    "config_env_expand",
    config_loader,
    "read_config",
    _make_patched_read_config,
    category="experience",
    target="config.config_loader.read_config",
    required_params=("config_path",),
    require_async=False,
    rebind_modules=True,
)
_patched_read_config = config_loader.read_config

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


install_mcp_token_injection_patch()


# 1c. Harden ESP32 18000 WebSocket, 18003 OTA, and 18003 vision_explain authentication (P0 #1)
def _extract_handler_allowed_devices(handler) -> set[str]:
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
                    auth_enabled = bool(getattr(self, "auth_enable", False))
                    hdrs = {str(k).lower(): str(v) for k, v in dict(request.headers).items()}
                    dev_raw = (hdrs.get("device-id") or "").strip()
                    client_raw = (hdrs.get("client-id") or "").strip()
                    dev_norm = _normalize_device_id(dev_raw)
                    if auth_enabled:
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
                            try:
                                self.logger.bind(tag="esp32_auth").warning(
                                    f"拒绝未授权设备 OTA 请求: device_id={dev_norm}"
                                )
                            except Exception:
                                pass
                            resp = _aiohttp_web.Response(
                                text=json.dumps(
                                    {"success": False, "message": "设备未在白名单中，拒绝下发配置"},
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
                    return resp

                ota_cls.handle_post = _patched_ota_handle_post
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

                    allowed = _extract_handler_allowed_devices(self)
                    if not dev_norm or not allowed or dev_norm not in allowed:
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


install_esp32_auth_hardening_patch()

# 2. Register custom Anima Brain memory provider without modifying container image
import core.utils.memory as memory_factory
import anima_memory_provider

def _wrap_create_memory(_orig):
    @functools.wraps(_orig)
    def _patched_create_memory(class_name, *args, **kwargs):
        if class_name == "anima_brain":
            return anima_memory_provider.MemoryProvider(*args, **kwargs)
        return _orig(class_name, *args, **kwargs)

    return _patched_create_memory


patch_registry.install_patch(
    "anima_memory_factory",
    memory_factory,
    "create_instance",
    _wrap_create_memory,
    category="experience",
    target="core.utils.memory.create_instance",
    required_params=("class_name",),
    require_async=False,
    rebind_modules=True,
)
_patched_create_memory = memory_factory.create_instance

# 3. Real-time per-turn async sync to Anima Brain + guaranteed <memory> injection
import core.utils.dialogue as dialogue_mod

_orig_put = dialogue_mod.Dialogue.put
_orig_get_llm_dialogue_with_memory = dialogue_mod.Dialogue.get_llm_dialogue_with_memory
_debounce_timers = {}  # key: id(dialogue) -> (threading.Timer, dialogue, str_req_id)
_timers_lock = threading.Lock()
_FALLBACK_DEBOUNCE_SECONDS = 1.5


def _cancel_dialogue_timer(dialogue) -> None:
    did = id(dialogue)
    with _timers_lock:
        entry = _debounce_timers.pop(did, None)
    if entry is not None:
        try:
            entry[0].cancel()
        except Exception:
            pass


def _push_pair_if_changed(dialogue, pair: dict) -> bool:
    if not pair:
        return False
    req_id = str(pair.get("request_id") or "")
    reply = str(pair.get("assistant_reply") or "")
    used_tools = bool(pair.get("used_tools", False))
    pushed_map = getattr(dialogue, "_anima_pushed_state", None)
    if not isinstance(pushed_map, dict):
        pushed_map = {}
        try:
            dialogue._anima_pushed_state = pushed_map
        except Exception:
            pass
    prev = pushed_map.get(req_id)
    if prev is not None:
        prev_reply, prev_tools = prev
        # Skip exact duplicate or shorter downgrade; allow pushing when reply grew or tool flag upgraded
        if len(reply) < len(prev_reply):
            return False
        if reply == prev_reply and (not used_tools or prev_tools):
            return False
    pushed_map[req_id] = (reply, used_tools)
    if len(pushed_map) > 128:
        oldest_key = next(iter(pushed_map))
        pushed_map.pop(oldest_key, None)
    anima_memory_provider.push_turns_async([pair])
    return True


def _find_latest_real_user(messages):
    for m in reversed(messages or []):
        if getattr(m, "is_temporary", False):
            continue
        if getattr(m, "role", "") == "user":
            txt = (getattr(m, "content", None) or "").strip()
            if txt and not txt.startswith("[系统提示]"):
                return m
    return None


def _mark_latest_user_attr(dialogue, attr: str, value) -> None:
    u = _find_latest_real_user(getattr(dialogue, "dialogue", None))
    if u is not None:
        try:
            setattr(u, attr, value)
        except Exception:
            pass


def _flush_dialogue_latest_pair(dialogue, *, messages_slice=None, mark_closed: bool = False) -> bool:
    msgs = dialogue.dialogue if messages_slice is None else messages_slice
    dev_id = str(getattr(dialogue, "_device_id", "") or "")
    pairs = anima_memory_provider.extract_clean_pairs(msgs, device_id=dev_id)
    if mark_closed:
        u = _find_latest_real_user(msgs)
        if u is not None:
            try:
                u._turn_closed = True
            except Exception:
                pass
    if not pairs:
        return False
    return _push_pair_if_changed(dialogue, pairs[-1])


def _flush_all_pending_dialogues() -> None:
    """Pre-exit hook: cancel all pending fallback timers and flush their latest turns into outbox."""
    with _timers_lock:
        entries = list(_debounce_timers.values())
        _debounce_timers.clear()
    for timer, dlg, _req_id in entries:
        try:
            timer.cancel()
        except Exception:
            pass
        try:
            _flush_dialogue_latest_pair(dlg)
        except Exception:
            pass


anima_memory_provider.register_pre_flush_hook(_flush_all_pending_dialogues)


@functools.wraps(_orig_put)
def _patched_put(self, message, *args, **kwargs):
    if not isinstance(getattr(message, "_occurred_at", None), (int, float)) or isinstance(
        getattr(message, "_occurred_at", None), bool
    ):
        try:
            message._occurred_at = time.time()
        except Exception:
            pass

    _orig_put(self, message, *args, **kwargs)
    try:
        if getattr(message, "is_temporary", False):
            return
        role = getattr(message, "role", "")
        content = (getattr(message, "content", None) or "").strip()
        dev_id = str(getattr(self, "_device_id", "") or "")

        # 1. A new real user utterance proves the previous turn is finished: flush previous turn immediately.
        if role == "user":
            if content.startswith("[系统提示]"):
                return
            if content:
                try:
                    runtime_events.publish_event(
                        "user_message",
                        text=content,
                        device_id=dev_id,
                    )
                except Exception:
                    pass
            _cancel_dialogue_timer(self)
            if len(self.dialogue) > 1:
                _flush_dialogue_latest_pair(self, messages_slice=self.dialogue[:-1], mark_closed=True)
            return

        # 2. Tool call or tool result means the current turn is mid-flight: never flush a half-reply yet.
        raw_tc = getattr(message, "tool_calls", None)
        has_tc = isinstance(raw_tc, (list, tuple)) and len(raw_tc) > 0
        if role == "tool" or (role == "assistant" and has_tc):
            _mark_latest_user_attr(self, "_turn_closed", False)
            _cancel_dialogue_timer(self)
            return

        if role != "assistant" or not content:
            return

        # 3. Inside ConnectionHandler.chat(): wait until depth=0 exits so multi-step tool turns commit complete replies.
        if int(getattr(self, "_in_chat_depth", 0) or 0) > 0:
            _cancel_dialogue_timer(self)
            try:
                import mei_emotion_router as _mer

                clean_ai = _mer.strip_emotion_tags(content).strip()
            except Exception:
                clean_ai = content
            if clean_ai:
                try:
                    runtime_events.publish_event(
                        "assistant_message",
                        text=clean_ai,
                        device_id=dev_id,
                        standalone=False,
                    )
                except Exception:
                    pass
            return

        # 3b. Standalone assistant greetings (e.g. helloHandle wakeup response / incoming call text)
        #     arriving outside chat() after a previous turn already closed must NOT corrupt the prior turn.
        latest_user = _find_latest_real_user(self.dialogue[:-1])
        if latest_user is not None and getattr(latest_user, "_turn_closed", False) is True:
            try:
                message._standalone_assistant = True
            except Exception:
                pass
            try:
                import mei_emotion_router as _mer

                clean_ai = _mer.strip_emotion_tags(content).strip()
            except Exception:
                clean_ai = content
            if clean_ai:
                try:
                    runtime_events.publish_event(
                        "assistant_message",
                        text=clean_ai,
                        device_id=dev_id,
                        standalone=True,
                    )
                except Exception:
                    pass
            return

        # 4. Outside chat() (e.g. direct intentHandler reply): emit event & schedule fallback debounce timer.
        try:
            import mei_emotion_router as _mer

            clean_ai = _mer.strip_emotion_tags(content).strip()
        except Exception:
            clean_ai = content
        if clean_ai:
            try:
                runtime_events.publish_event(
                    "assistant_message",
                    text=clean_ai,
                    device_id=dev_id,
                    standalone=False,
                )
            except Exception:
                pass
        pairs = anima_memory_provider.extract_clean_pairs(self.dialogue, device_id=dev_id)
        if not pairs:
            return
        latest_pair = pairs[-1]
        req_id = latest_pair["request_id"]
        did = id(self)

        def _flush(dlg=self, key=did):
            with _timers_lock:
                _debounce_timers.pop(key, None)
            _flush_dialogue_latest_pair(dlg, mark_closed=True)

        with _timers_lock:
            old_entry = _debounce_timers.pop(did, None)
            if old_entry is not None:
                old_entry[0].cancel()
            t = threading.Timer(_FALLBACK_DEBOUNCE_SECONDS, _flush)
            t.daemon = True
            _debounce_timers[did] = (t, self, req_id)
            t.start()
    except Exception:
        pass


@functools.wraps(_orig_get_llm_dialogue_with_memory)
def _patched_get_llm_dialogue_with_memory(self, *args, **kwargs):
    if args:
        memory_str = args[0]
        fwd_args = (None,) + tuple(args[1:])
        fwd_kwargs = dict(kwargs)
    else:
        memory_str = kwargs.get("memory_str")
        fwd_args = ()
        fwd_kwargs = dict(kwargs)
        fwd_kwargs["memory_str"] = None
    # Pass memory_str=None to upstream so upstream's non-anchored re.sub('<memory>.*?</memory>')
    # cannot swallow <core_rules>/<tool_usage> if persona prose mentions '<memory>' inline;
    # turn_context_router handles memory_str safely in both split and legacy modes.
    msgs = _orig_get_llm_dialogue_with_memory(self, *fwd_args, **fwd_kwargs)
    try:
        return turn_context_router.apply_turn_context_to_messages(
            msgs,
            memory_str=memory_str,
        )
    except Exception:
        if memory_str and msgs and msgs[0].get("role") == "system":
            if memory_str not in msgs[0].get("content", ""):
                msgs[0]["content"] += f"\n\n<memory>\n{memory_str}\n</memory>"
        try:
            import reply_length_router

            reply_length_router.inject_reply_length_into_messages(msgs)
        except Exception:
            pass
        return msgs


if getattr(dialogue_mod.Dialogue, "_anima_dialogue_patched", False):
    if patch_registry.get_patch_status("anima_dialogue_hooks") is None:
        patch_registry.record_patch(
            "anima_dialogue_hooks",
            "ok",
            target="Dialogue.put/get_llm_dialogue_with_memory",
            detail="already applied",
            contract_ok=True,
            category="experience",
        )
else:
    _ok_put, _det_put = patch_registry.verify_signature_contract(
        _orig_put, required_params=("self", "message"), require_async=False
    )
    _ok_mem_d, _det_mem_d = patch_registry.verify_signature_contract(
        _orig_get_llm_dialogue_with_memory, required_params=("self", "memory_str"), require_async=False
    )
    if not (_ok_put and _ok_mem_d):
        patch_registry.record_patch(
            "anima_dialogue_hooks",
            "skipped_contract",
            target="Dialogue.put/get_llm_dialogue_with_memory",
            detail=f"{_det_put}; {_det_mem_d}",
            contract_ok=False,
            category="experience",
        )
        print(f"[AnimaDialogue] 跳过挂载对话钩子（签名契约不匹配）: {_det_put}; {_det_mem_d}", flush=True)
    else:
        dialogue_mod.Dialogue.put = _patched_put
        dialogue_mod.Dialogue.get_llm_dialogue_with_memory = _patched_get_llm_dialogue_with_memory
        dialogue_mod.Dialogue._anima_dialogue_patched = True
        patch_registry.record_patch(
            "anima_dialogue_hooks",
            "ok",
            target="Dialogue.put/get_llm_dialogue_with_memory",
            detail=f"{_det_put}; {_det_mem_d}",
            contract_ok=True,
            category="experience",
        )


def install_chat_turn_flush_patch() -> None:
    """Wrap ConnectionHandler.chat so Anima memory flushes once the full turn (including tool rounds) finishes."""
    try:
        from core.connection import ConnectionHandler

        if getattr(ConnectionHandler, "_anima_turn_flush_patched", False):
            if patch_registry.get_patch_status("anima_turn_flush") is None:
                patch_registry.record_patch("anima_turn_flush", "ok", target="ConnectionHandler.chat", detail="already applied")
            return
        _orig_chat = ConnectionHandler.chat
        ok_chat, det_chat = patch_registry.verify_signature_contract(
            _orig_chat, required_params=("self", "query"), require_async=False
        )
        ok_fn = True
        det_fn = "not_present"
        has_handle_fn = hasattr(ConnectionHandler, "_handle_function_result")
        _orig_handle_fn = ConnectionHandler._handle_function_result if has_handle_fn else None
        if has_handle_fn:
            ok_fn, det_fn = patch_registry.verify_signature_contract(
                _orig_handle_fn, required_params=("self", "tool_results"), require_async=False
            )

        if not (ok_chat and ok_fn):
            patch_registry.record_patch(
                "anima_turn_flush",
                "skipped_contract",
                target="ConnectionHandler.chat/_handle_function_result",
                detail=f"{det_chat}; {det_fn}",
                contract_ok=False,
                category="experience",
            )
            print(
                f"[AnimaTurnFlush] 跳过挂载 ConnectionHandler.chat 补丁（签名契约不匹配）: {det_chat}; {det_fn}",
                flush=True,
            )
            return

        @functools.wraps(_orig_chat)
        def _patched_chat(self, *args, **kwargs):
            dlg = getattr(self, "dialogue", None)
            dev_id = str(getattr(self, "device_id", "") or "")
            if dlg is not None:
                if dev_id:
                    try:
                        dlg._device_id = dev_id
                    except Exception:
                        pass
                try:
                    dlg._in_chat_depth = int(getattr(dlg, "_in_chat_depth", 0) or 0) + 1
                except Exception:
                    pass
            chat_res = None
            chat_raised = False
            try:
                chat_res = _orig_chat(self, *args, **kwargs)
                return chat_res
            except Exception:
                chat_raised = True
                raise
            finally:
                if dlg is not None:
                    new_depth = 0
                    try:
                        new_depth = max(0, int(getattr(dlg, "_in_chat_depth", 1) or 1) - 1)
                        dlg._in_chat_depth = new_depth
                    except Exception:
                        new_depth = 0
                    if new_depth == 0:
                        _cancel_dialogue_timer(dlg)
                        try:
                            _flush_dialogue_latest_pair(dlg, mark_closed=True)
                        except Exception:
                            pass
                        if (
                            (chat_raised or chat_res is None)
                            and not getattr(self, "client_is_speaking", False)
                            and getattr(self, "_pc_audio_active_sid", None) is None
                        ):
                            try:
                                runtime_events.publish_event(
                                    "tts_state",
                                    state="stop",
                                    text=None,
                                    device_id=dev_id,
                                    sentence_id=str(getattr(self, "sentence_id", "") or ""),
                                    reason="chat_error",
                                )
                            except Exception:
                                pass

        ConnectionHandler.chat = _patched_chat

        if has_handle_fn and _orig_handle_fn is not None:
            @functools.wraps(_orig_handle_fn)
            def _patched_handle_function_result(self, tool_results, *args, **kwargs):
                dlg = getattr(self, "dialogue", None)
                if dlg is not None and tool_results:
                    _mark_latest_user_attr(dlg, "_used_tools", True)
                return _orig_handle_fn(self, tool_results, *args, **kwargs)

            ConnectionHandler._handle_function_result = _patched_handle_function_result

        ConnectionHandler._anima_turn_flush_patched = True
        patch_registry.record_patch(
            "anima_turn_flush",
            "ok",
            target="ConnectionHandler.chat/_handle_function_result",
            detail=f"{det_chat}; {det_fn}",
            contract_ok=True,
            category="experience",
        )
    except Exception as _chat_exc:
        patch_registry.record_patch(
            "anima_turn_flush",
            "failed",
            target="ConnectionHandler.chat",
            detail=str(_chat_exc),
            contract_ok=False,
            category="experience",
        )
        print(f"[AnimaTurnFlush] ConnectionHandler.chat 补丁跳过: {_chat_exc}", flush=True)


# 4-5. Install experience patches via unified run_patch_installer (C-3)
def _install_nmc_weather():
    import nmc_weather_patch

    nmc_weather_patch.apply_nmc_weather_patch()


def _install_pc_audio_tts():
    import pc_audio_router

    pc_audio_router.apply_pc_audio_to_tts_patch()


def _install_mei_emotion():
    import mei_emotion_router

    mei_emotion_router.apply_xiaozhi_emotion_and_prosody_patches()


def _install_pc_audio_playback():
    import pc_audio_router

    pc_audio_router.apply_pc_audio_playback_patches()


def _install_mei_wakeup():
    import mei_emotion_router

    mei_emotion_router.apply_mei_wakeup_responses_patch()


def _install_idle_farewell():
    import mei_emotion_router

    mei_emotion_router.apply_idle_farewell_patch()


def _install_reply_length():
    import reply_length_router

    reply_length_router.apply_reply_length_llm_patch()


def _install_llm_prefix_cache():
    turn_context_router.apply_llm_prefix_cache_patch()


for _pname, _pfn, _ptarget, _pprefix in (
    ("nmc_weather", _install_nmc_weather, "get_weather", "[NMCWeather]"),
    ("pc_audio_tts", _install_pc_audio_tts, "TTSProviderBase.to_tts_stream", "[PCAudioRouter]"),
    ("mei_emotion_and_prosody", _install_mei_emotion, "mei_emotion_router", "[MeiEmotionRouter]"),
    ("anima_turn_flush", install_chat_turn_flush_patch, "ConnectionHandler.chat", "[AnimaTurnFlush]"),
    ("pc_audio_playback", _install_pc_audio_playback, "sendAudioHandle", "[PCAudioRouter]"),
    ("mei_wakeup_responses", _install_mei_wakeup, "helloHandle", "[MeiEmotionRouter]"),
    ("idle_farewell", _install_idle_farewell, "receiveAudioHandle.no_voice_close_connect", "[MeiEmotionRouter]"),
    ("reply_length_llm", _install_reply_length, "OpenAI.LLMProvider", "[ReplyLengthRouter]"),
    ("llm_prefix_cache", _install_llm_prefix_cache, "OpenAI.LLMProvider.__init__", "[LLMCache]"),
):
    patch_registry.run_patch_installer(
        _pname, _pfn, target=_ptarget, category="experience", log_prefix=_pprefix
    )

# Enable structured runtime event ring buffer for Console (replaces Chinese log regex parsing, P1-11)
runtime_events.mark_hooks_active(True)


# 6. Memory optimization for Sherpa-ONNX local ASR + periodic glibc heap trim
def _install_memory_optimizations():
    import asyncio
    import ctypes
    import gc
    import time
    import types

    # 6a. Lazy-stub modelscope.hub.file_download when Sherpa-ONNX model files already exist locally.
    #     Eagerly importing modelscope drags in PyTorch/scipy/datasets (~330MB RSS) even when unused.
    sherpa_dir = os.path.join(ROOT_DIR, "models", "sherpa-onnx-sense-voice")
    has_local_sherpa = os.path.isfile(os.path.join(sherpa_dir, "model.int8.onnx")) and os.path.isfile(
        os.path.join(sherpa_dir, "tokens.txt")
    )
    if has_local_sherpa and "modelscope" not in sys.modules:
        _ms_pkg = types.ModuleType("modelscope")
        _ms_pkg.__path__ = []
        _ms_hub = types.ModuleType("modelscope.hub")
        _ms_hub.__path__ = []
        _ms_dl = types.ModuleType("modelscope.hub.file_download")

        def _lazy_model_file_download(*args, **kwargs):
            for mod_name in ("modelscope.hub.file_download", "modelscope.hub", "modelscope"):
                if sys.modules.get(mod_name) in (_ms_dl, _ms_hub, _ms_pkg):
                    sys.modules.pop(mod_name, None)
            from modelscope.hub.file_download import model_file_download as _real_download
            return _real_download(*args, **kwargs)

        _ms_dl.model_file_download = _lazy_model_file_download
        _ms_hub.file_download = _ms_dl
        _ms_pkg.hub = _ms_hub
        sys.modules["modelscope"] = _ms_pkg
        sys.modules["modelscope.hub"] = _ms_hub
        sys.modules["modelscope.hub.file_download"] = _ms_dl

    # 6b. Offload Sherpa-ONNX CPU decode_stream to worker thread so asyncio loop stays responsive
    #     Always installed (including first-boot auto-download when local files don't exist yet).
    try:
        import core.providers.asr.sherpa_onnx_local as _sherpa_mod

        _TAG_RE = re.compile(r"<\|[^|]+\|>")

        def _wrap_speech_to_text(_orig_stt):
            @functools.wraps(_orig_stt)
            async def _async_speech_to_text(self, opus_data, session_id, artifacts=None, *args, **kwargs):
                file_path = None
                try:
                    if artifacts is None:
                        return "", None
                    file_path = artifacts.file_path
                    t_asr_start = time.monotonic()

                    def _decode():
                        s = self.model.create_stream()
                        samples, sample_rate = self.read_wave(file_path)
                        s.accept_waveform(sample_rate, samples)
                        self.model.decode_stream(s)
                        return s.result.text

                    text = await asyncio.to_thread(_decode)
                    asr_sec = time.monotonic() - t_asr_start
                    if text:
                        text = _TAG_RE.sub("", text).strip()
                    _sherpa_mod.logger.bind(tag=_sherpa_mod.TAG).debug(
                        f"语音识别耗时: {asr_sec:.3f}s | 结果: {text}"
                    )
                    return text, file_path
                except Exception as exc:
                    _sherpa_mod.logger.bind(tag=_sherpa_mod.TAG).error(
                        f"语音识别失败: {exc}", exc_info=True
                    )
                    return "", file_path

            return _async_speech_to_text

        patch_registry.install_patch(
            "sherpa_async_asr",
            _sherpa_mod.ASRProvider,
            "speech_to_text",
            _wrap_speech_to_text,
            category="experience",
            target="SherpaASR.ASRProvider.speech_to_text",
            required_params=("self", "opus_data", "session_id"),
            require_async=True,
            flag_obj=_sherpa_mod.ASRProvider,
            flag_attr="_sherpa_async_patched",
        )
    except Exception as _s_exc:
        patch_registry.record_patch(
            "sherpa_async_asr",
            "failed",
            target="SherpaASR.ASRProvider.speech_to_text",
            detail=str(_s_exc),
            contract_ok=False,
            category="experience",
        )
        print(f"[MemOpt] SherpaASR 异步补丁跳过: {_s_exc}", flush=True)

    # 6c. Periodic glibc malloc_trim(0) so transient Python/ONNX heap returns to Linux/WSL2
    try:
        _libc = ctypes.CDLL("libc.so.6")
        _malloc_trim = _libc.malloc_trim
        _malloc_trim.argtypes = [ctypes.c_size_t]
        _malloc_trim.restype = ctypes.c_int

        def _trim_loop():
            time.sleep(8)
            while True:
                try:
                    gc.collect()
                    _malloc_trim(0)
                except Exception:
                    pass
                time.sleep(180)

        threading.Thread(target=_trim_loop, name="glibc-malloc-trim", daemon=True).start()
    except Exception:
        pass


try:
    _install_memory_optimizations()
except Exception as _opt_exc:
    print(f"[MemOpt] 初始化异常: {_opt_exc}", flush=True)

def enforce_security_boot_gate(exit_code: int = 2) -> list[dict]:
    """Fail-closed gate (C-1): exit(2) when any required security patch failed or is missing,
    unless XIAOZHI_ALLOW_INSECURE_BOOT=1."""
    return patch_registry.enforce_security_patches_or_exit(
        exit_code=exit_code,
        required_security_patches=patch_registry.SECURITY_PATCH_NAMES,
    )


if __name__ == "__main__":
    enforce_security_boot_gate(exit_code=2)
    try:
        import console_server
        console_server.start_console_background()
    except Exception as exc:
        print(f"[WebConsole] 启动异常: {exc}", flush=True)
    runpy.run_path(os.path.join(ROOT_DIR, "app.py"), run_name="__main__")
