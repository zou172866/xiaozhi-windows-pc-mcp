"""Voiceprint & screen-peek MCP tool permission guard patch (#22, Phase 2 ④/⑤).

Zero import-time side effects: call `install_voiceprint_mcp_guard_patch()` explicitly from `boot.install_all()`.
"""

from __future__ import annotations

import functools

import patch_registry
import runtime_events
import voiceprint_router


def install_voiceprint_mcp_guard_patch() -> bool:
    """Wrap `ServerMCPManager.execute_tool` to enforce owner-only host MCP tools under voiceprint full_guard
    and honor the on-demand `screen_peek.enabled` toggle."""
    try:
        from core.providers.tools.server_mcp import mcp_manager as _mcp_mgr_mod

        cls = _mcp_mgr_mod.ServerMCPManager

        def _wrap_execute_tool(_orig_exec):
            @functools.wraps(_orig_exec)
            async def _guarded_execute_tool(self, tool_name, arguments, *args, **kwargs):
                conn = getattr(self, "conn", None)
                sid = str(getattr(conn, "session_id", "") or "").strip()
                tname = str(tool_name or "")
                try:
                    allowed, deny_msg = voiceprint_router.check_mcp_tool_permission(sid, tname)
                except Exception as exc:
                    print(f"[VoiceprintGuard] WARNING: 声纹权限校验异常，已安全拦截 {tname}: {exc}", flush=True)
                    allowed = False
                    deny_msg = "【声纹安全拦截】声纹权限校验暂时异常，为保护电脑安全已暂缓执行该工具。"
                if not allowed:
                    try:
                        runtime_events.publish_event(
                            "voiceprint_mcp_blocked",
                            session_id=sid,
                            tool_name=tname,
                            reason=deny_msg[:160],
                        )
                    except Exception:
                        pass
                    return deny_msg
                return await _orig_exec(self, tool_name, arguments, *args, **kwargs)

            return _guarded_execute_tool

        already_applied = bool(getattr(cls, "_voiceprint_mcp_guard_patched", False))
        ok, _ = patch_registry.install_patch(
            "voiceprint_mcp_guard",
            cls,
            "execute_tool",
            _wrap_execute_tool,
            category="experience",
            target="ServerMCPManager.execute_tool",
            required_params=("self", "tool_name", "arguments"),
            require_async=True,
            flag_obj=cls,
            flag_attr="_voiceprint_mcp_guard_patched",
        )
        if ok and not already_applied:
            print("[VoiceprintGuard] 已挂载声纹与按需浅看屏 MCP 权限拦截补丁", flush=True)
        return bool(ok)
    except Exception as exc:
        patch_registry.record_patch(
            "voiceprint_mcp_guard",
            "failed",
            target="ServerMCPManager.execute_tool",
            detail=f"{type(exc).__name__}: {exc}",
            contract_ok=False,
            category="experience",
        )
        print(f"[VoiceprintGuard] 挂载 MCP 权限补丁失败: {exc}", flush=True)
        return False
