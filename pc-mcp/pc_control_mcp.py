"""XiaoZhi Windows 3-Tier PC Control MCP + 48kHz WinMM Audio Bridge entrypoint (P2-20).

Thin facade over `pc_mcp_modules/{winapi,security,launcher,vision,ui_tools,audio_bridge}`
preserving full backward-compatible exports for tests and 18900 service startup.
"""

from __future__ import annotations

import atexit
import ctypes
import os
from pathlib import Path
import sys
import time

# 必须在导入 pyautogui / PIL / uiautomation 之前开启 Per-Monitor DPI V2 感知（1:1 真实物理像素）
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

# 统一所有线程的 COM 套间为 MTA (COINIT_MULTITHREADED)，防止 pycaw (STA) 与 UIA (MTA) 混用引发 RPC_E_CHANGED_MODE
sys.coinit_flags = 0

_MCP_DIR = Path(__file__).resolve().parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import l3_backends
import pyautogui
import pyperclip
import uvicorn
from PIL import ImageGrab
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

pyautogui.FAILSAFE = os.environ.get("PC_MCP_DISABLE_FAILSAFE", "").strip().lower() not in ("1", "true", "yes")
pyautogui.PAUSE = 0.08

from pc_mcp_modules import (  # noqa: E402
    ACTIVE_DIR,
    CONFIG_YAML,
    CONTAINER_TOKENS_ENV_FILE,
    DATA_DIR,
    HEARTBEAT_FILE,
    L2_AUDIT_LOG_FILE,
    LAST_GROUNDING_IMG,
    LEGACY_TOKEN_FILE,
    LOG_FILE,
    LOGS_DIR,
    MODE_FILE,
    PENDING_DIR,
    RUNTIME_SETTINGS_FILE,
    SECRETS_DIR,
    SERVER_DIR,
    STATE_DIR,
    TASKS_ROOT,
    TOKEN_FILE,
    _CF_UNICODETEXT,
    _RPC_E_CHANGED_MODE,
    _audit_log_lock,
    _clipboard_lock,
    _com_mta_scope,
    _get_env_key,
    _gui_lock,
)
from pc_mcp_modules.audio_bridge import (  # noqa: E402
    _ALLOWED_L3_CONFIG_KEYS,
    _WAVEFORMATEX,
    _WAVEHDR,
    _WinMMPlayer,
    _get_soft_limiter_lut,
    _scale_pcm16,
    _validate_l3_config_payload,
    _winmm,
    register_http_routes,
)
from pc_mcp_modules.launcher import (  # noqa: E402
    _BROWSER_PROCS,
    _BUILTIN_APPS,
    _COMMON_WEBSITES,
    _find_start_menu_shortcut,
    _get_default_browser_proc,
    _pc_open_app_or_web_locked,
    _shell_open,
)
from pc_mcp_modules.security import (  # noqa: E402
    _ACTIVE_BEARER_TOKEN,
    _BLOCKED_EXACT_HOTKEYS,
    _BLOCKED_HOTKEY_COMBOS,
    _BLOCKED_INPUT_PROCESSES,
    _BLOCKED_INPUT_WINDOW_CLASSES,
    _BLOCKED_SUBSET_HOTKEYS,
    _CREATE_TASK_DIALOG_TITLES,
    _HOTKEY_ALIAS_MAP,
    _RUN_DIALOG_TITLES,
    _SECRET_PATTERNS,
    _VALID_MODES,
    _AccessLogNoiseFilter,
    _LocalBearerAndHostMiddleware,
    _SecretRedactingFilter,
    _build_allowed_hosts_and_origins,
    _check_click_guard,
    _check_foreground_hotkey_guard,
    _check_foreground_input_guard,
    _check_hotkey_blacklist,
    _check_permission,
    _configure_logging,
    _get_audit_extra_secrets,
    _get_mode,
    _is_explorer_address_or_search_bar,
    _parse_and_normalize_hotkey,
    _sanitize_audit_text,
    _summarize_typed_text_for_audit,
    _validate_hotkey_keys,
    _write_l2_audit_log,
    EmergencyStopLockedError,
    clear_emergency_stop,
    is_emergency_stop_active,
    load_or_create_pc_mcp_token as _load_or_create_pc_mcp_token_impl,
    redact_secrets,
    set_active_bearer_token,
    set_host_mode,
    start_emergency_stop_hotkey_listener,
    stop_emergency_stop_hotkey_listener,
    trigger_emergency_stop,
)
from pc_mcp_modules.ui_tools import (  # noqa: E402
    _clear_clipboard,
    _pc_ui_action_locked,
    inspect_window_ui,
)
from pc_mcp_modules.vision import (  # noqa: E402
    LocateResult,
    _POSITIONAL_HINTS,
    _call_vision_llm,
    _collect_uia_elements,
    _compute_region_hash,
    _draw_som_overlay,
    _parse_json_obj,
    _parse_mark_id,
    _prefer_gemini_vision,
    _region_hashes_match,
    _smart_locate_target,
    _walk_uia_controls,
    look_at_screen,
)
from pc_mcp_modules.winapi import (  # noqa: E402
    SMTO_ABORTIFHUNG,
    SMTO_NORMAL,
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV_TRACK,
    VK_VOLUME_MUTE,
    _broadcast_screen_off,
    _dwmapi,
    _force_foreground_window,
    _get_focused_control_info,
    _get_foreground_window_info,
    _get_process_name_by_pid,
    _get_window_info_at_point,
    _get_window_info_by_hwnd,
    _get_window_title,
    _is_window_cloaked,
    _kernel32,
    _list_top_windows,
    _set_or_get_volume,
    _shell32,
    _tap_vk,
    _user32,
    check_system_status,
    run_system_control,
)


def load_or_create_pc_mcp_token(token_file: Path | None = None, *, retries: int = 3) -> str:
    global _ACTIVE_BEARER_TOKEN
    tok = _load_or_create_pc_mcp_token_impl(token_file=token_file, retries=retries)
    _ACTIVE_BEARER_TOKEN = tok
    return tok


mcp = MCPServer("xiaozhi-pc-control")


@mcp.tool()
def pc_system_control(action: str, volume_percent: int = 30) -> str:
    """[L1 瞬时系统控制] 控制电脑音量、静音、音乐视频播放切歌、锁屏或息屏（不抢夺鼠标焦点）。
    action 可选值:
    - set_volume: 设置音量百分比 (0-100)，配合 volume_percent 参数
    - get_volume: 查询当前音量
    - mute / unmute: 静音 / 取消静音
    - media_play_pause: 播放或暂停当前音乐/视频
    - media_next / media_prev: 下一首 / 上一首
    - lock_screen: 锁定电脑屏幕
    - screen_off: 关闭显示器背光息屏
    """
    err = _check_permission("L1")
    if err:
        return err
    return run_system_control(action, volume_percent)


@mcp.tool()
def pc_open_app_or_web(target: str) -> str:
    """[L1 打开软件或网页并弹出到最前] 按名称启动或切出电脑上的应用程序（如浏览器、微信、VS Code、记事本、计算器等），或打开网页/常见网站（如 B站、百度、抖音，或直接传 https:// 网址），并自动将对应窗口恢复并置顶弹出到屏幕最前方。"""
    err = _check_permission("L1")
    if err:
        return err

    t = (target or "").strip().rstrip("。！!？?，, ")
    if not t:
        return "未指定要打开的目标。"
    with _gui_lock:
        return _pc_open_app_or_web_locked(t)


@mcp.tool()
def pc_look_at_screen(question: str = "请简明描述当前电脑屏幕上正在显示的主要内容。") -> str:
    """[L1 屏幕视觉识别] 仅当用户询问“看看我电脑屏幕上是什么 / 这个报错怎么回事 / 读一下屏幕内容”时调用。注意：如果要点击界面元素，请直接调用 pc_ui_action 并传入 element_name，不要先调本工具猜坐标！"""
    err = _check_permission("L1")
    if err:
        return err
    return look_at_screen(question)


@mcp.tool()
def pc_check_system_status() -> str:
    """[L1 电脑状态查询] 查询电脑当前 CPU 占用、内存使用率、显卡温度与显存占用，以及当前前台打开的窗口名称。"""
    err = _check_permission("L1")
    if err:
        return err
    return check_system_status()


@mcp.tool()
def pc_inspect_window_ui(window_keyword: str = "") -> str:
    """[L2 窗口组件读取] 读取当前前台窗口（或标题包含 window_keyword 的窗口）内的可点击按钮、网页链接、输入框等 UI 元素名称与真实物理坐标（需全权操控挡）。"""
    err = _check_permission("L2")
    if err:
        _write_l2_audit_log(
            tool="pc_inspect_window_ui",
            action="inspect_window_ui",
            outcome="blocked",
            reason="emergency_stop" if is_emergency_stop_active() else "mode_blocked",
            target=window_keyword,
            detail=err,
        )
        return err
    if is_emergency_stop_active():
        msg = "安全急停已触发（Ctrl+Alt+Pause），已立即中止 L2 窗口组件读取，请在宿主机侧显式复位急停后再试。"
        _write_l2_audit_log(
            tool="pc_inspect_window_ui",
            action="inspect_window_ui",
            outcome="blocked",
            reason="emergency_stop",
            target=window_keyword,
            detail=msg,
        )
        return msg
    return inspect_window_ui(window_keyword)


@mcp.tool()
def pc_ui_action(
    action: str,
    element_name: str = "",
    x: int = -1,
    y: int = -1,
    text: str = "",
    press_enter: bool = False,
    hotkey: str = "",
    scroll_clicks: int = -5,
) -> str:
    """[L2 高精度界面点击与键鼠输入] 在电脑窗口或网页中精准点击元素、输入文字、按快捷键或滚轮翻页（需在 18080 控制台开启全权操控挡）。
    【重要调用准则】：点击任何按钮、网页视频封面、输入框或图标时，请直接把目标描述传给 element_name（例如 '第一个视频封面'、'搜索输入框'、'全屏按钮' 或具体标题文字），本工具内部已内置 1:1 物理像素 UIA + Set-of-Mark 多模态视觉精确定位！严禁自己先调 pc_look_at_screen 估算 x, y 像素盲点！
    action 可选:
    - click / double_click / right_click: 传入 element_name 自动精确定位并点击/双击/右键（仅在已知精确物理像素时才传 x, y）
    - type_text: 输入中英文文字 text（若传了 element_name 会先精确定位并点击该输入框；未定位到目标时会直接报错中止，不会误输入到当前焦点窗口；press_enter=True 则输完自动按回车）
    - hotkey: 发送快捷键，如 'ctrl+c'、'alt+tab'、'space'、'enter'、'esc'（已禁止 win+r / win+x / ctrl+shift+esc 等高危系统快捷键）
    - scroll: 滚动鼠标滚轮，scroll_clicks 负数向下滚，正数向上滚
    """
    act = (action or "").strip().lower()
    err = _check_permission("L2")
    if err:
        _write_l2_audit_log(
            tool="pc_ui_action",
            action=act,
            outcome="blocked",
            reason="emergency_stop" if is_emergency_stop_active() else "mode_blocked",
            target=element_name or (f"({x},{y})" if x >= 0 and y >= 0 else ""),
            hotkey=hotkey,
            text=text,
            press_enter=press_enter,
            detail=err,
        )
        return err

    # E-3: _pc_ui_action_locked manages _gui_lock fine-grained so VLM inference runs outside _gui_lock.
    return _pc_ui_action_locked(
        act=act,
        raw_action=action,
        element_name=element_name,
        x=x,
        y=y,
        text=text,
        press_enter=press_enter,
        hotkey=hotkey,
        scroll_clicks=scroll_clicks,
    )


@mcp.tool()
def pc_agent_task(
    action: str = "dispatch",
    task_instruction: str = "",
    follow_up_last: bool = False,
) -> str:
    """[L3 本地后台 Agent 重装大脑] 当用户要求执行复杂多步电脑工程任务（如整理归档文件夹、编写/修改代码、深度调研生成报告、运行自动化工作流）或明确提到“反重力”、“DSH”、“WorkBuddy”、“后台处理”时调用（仅在 18080 控制台【全权操控挡】(full) 下可用）。
    后端引擎可在 18080 控制台免重启切换（支持 Google Antigravity / DeepSeek DSH / 腾讯 WorkBuddy / 关闭）。
    action 可选:
    - dispatch: 异步派发新任务给当前已选的本地 L3 后台 Agent 执行（follow_up_last=True 则向最近一次派发的任务追加指令）
    - check_status: 查询最近一次派发任务的执行进度与结果摘要
    - cancel: 取消当前工作目录正在进行或排队中的后台任务
    """
    err = _check_permission("L3")
    if err:
        return err

    act = (action or "dispatch").strip().lower()
    if act == "check_status":
        return l3_backends.check_task_status()
    if act == "cancel":
        return l3_backends.cancel_active_task()

    instr = (task_instruction or "").strip()
    if not instr:
        return "任务内容不能为空。"
    return l3_backends.dispatch_task(instr, follow_up_last=bool(follow_up_last))


_pc_audio_player = _WinMMPlayer()
register_http_routes(mcp, _pc_audio_player)


def create_secured_asgi_app(token: str, port: int = 18900, bind_host: str = "127.0.0.1"):
    global _ACTIVE_BEARER_TOKEN
    if token:
        _ACTIVE_BEARER_TOKEN = token
        set_active_bearer_token(token)
    allowed_hosts, allowed_origins = _build_allowed_hosts_and_origins(port)
    sec_settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    inner_app = mcp.streamable_http_app(
        transport_security=sec_settings,
        host=bind_host,
    )
    return _LocalBearerAndHostMiddleware(inner_app, token, allowed_hosts, allowed_origins)


if __name__ == "__main__":
    bind_host = (os.environ.get("PC_MCP_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    bind_port = int(os.environ.get("PC_MCP_PORT") or "18900")
    bearer_token = load_or_create_pc_mcp_token()
    log = _configure_logging(bearer_token)
    start_emergency_stop_hotkey_listener()
    atexit.register(stop_emergency_stop_hotkey_listener)
    try:
        reaped = l3_backends.reap_stale_running_tasks()
        if reaped:
            log.info("Startup reaped %d stale zombie running L3 task(s).", reaped)
    except Exception as exc:
        log.warning("Startup L3 task reap encountered an isolated error (continuing boot): %s", exc)
    log.info(
        "[xiaozhi-pc-control] Starting secured L1+L2+L3 MCP + WinMM PC Audio Bridge on %s:%d (Bearer auth + DNS rebinding protection enabled)",
        bind_host,
        bind_port,
    )
    app = create_secured_asgi_app(bearer_token, port=bind_port, bind_host=bind_host)
    uvicorn.run(app, host=bind_host, port=bind_port, log_config=None)
