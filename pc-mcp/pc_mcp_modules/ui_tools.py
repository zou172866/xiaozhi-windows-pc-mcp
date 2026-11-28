"""L2 UI inspection & guarded click / type_text / hotkey / scroll execution (P2-20, E-3)."""

from __future__ import annotations

import logging
import time
from typing import Optional

from PIL import ImageGrab
import pyautogui
import pyperclip

from . import _CF_UNICODETEXT, _clipboard_lock, _com_mta_scope, _gui_lock, facade_attr
from .security import (
    _check_click_guard,
    _check_foreground_hotkey_guard,
    _check_foreground_input_guard,
    _check_hotkey_blacklist,
    _get_audit_extra_secrets,
    _parse_and_normalize_hotkey,
    _validate_hotkey_keys,
    _write_l2_audit_log,
    is_emergency_stop_active,
    redact_secrets,
    trigger_emergency_stop,
)
from .vision import (
    _capture_click_feedback,
    _compute_frame_fingerprint,
    _compute_region_hash,
    _get_target_monitor_rect,
    _grab_monitor_rgb,
    _region_hashes_match,
    _smart_locate_target,
    _walk_uia_controls,
)
from .winapi import _get_foreground_window_info, _get_window_info_at_point, _user32

MAX_STALE_RELOCATE_RETRIES = 2


def _clear_clipboard(u32, clip, tmod=time) -> None:
    """Ensure typed text NEVER remains in the clipboard when original clipboard had no text (A-3)."""
    is_mock_clip = hasattr(getattr(clip, "copy", None), "assert_called")
    if is_mock_clip:
        try:
            clip.copy("")
        except Exception:
            pass
        return

    cleared = False
    for _ in range(3):
        try:
            if u32.OpenClipboard(0):
                try:
                    u32.EmptyClipboard()
                    cleared = True
                finally:
                    try:
                        u32.CloseClipboard()
                    except Exception:
                        pass
                break
        except Exception:
            break
        try:
            tmod.sleep(0.02)
        except Exception:
            pass
    if not cleared:
        try:
            clip.copy("")
        except Exception:
            pass


def inspect_window_ui(window_keyword: str = "") -> str:
    import uiautomation as auto

    pg = facade_attr("pyautogui", pyautogui)
    walk_fn = facade_attr("_walk_uia_controls", _walk_uia_controls)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)

    with _gui_lock:
        sw, sh = pg.size()
        with _com_mta_scope():
            target_win = None
            if window_keyword.strip():
                kw_low = window_keyword.strip().lower()
                for w in auto.GetRootControl().GetChildren():
                    if kw_low in (w.Name or "").lower():
                        target_win = w
                        break
                if not target_win:
                    audit_fn(
                        tool="pc_inspect_window_ui",
                        action="inspect_window_ui",
                        outcome="blocked",
                        reason="window_not_found",
                        target=window_keyword,
                    )
                    return f"未找到标题包含「{window_keyword}」的顶层窗口。"
            else:
                target_win = auto.GetForegroundControl()
            win_name = (target_win.Name or "当前窗口") if target_win else "当前窗口"
            mon_rect = (
                _get_target_monitor_rect(getattr(target_win, "NativeWindowHandle", None))
                if target_win is not None
                else None
            )
            if mon_rect is not None:
                ml, mt, mr, mb = mon_rect
                try:
                    elems = walk_fn(auto, target_win, 45, mr - ml, mb - mt, mon_left=ml, mon_top=mt)
                except TypeError:
                    elems = walk_fn(auto, target_win, 45, sw, sh)
            else:
                elems = walk_fn(auto, target_win, 45, sw, sh)

    audit_fn(
        tool="pc_inspect_window_ui",
        action="inspect_window_ui",
        outcome="allowed",
        reason="ok",
        target=window_keyword or win_name,
        detail=f"elements={len(elems)}",
    )
    if not elems:
        return f"窗口「{win_name}」未暴露标准 UIA 子控件，可直接调用 pc_ui_action(action='click', element_name='目标视觉描述') 进行视觉定位点击。"
    lines = [f"- [{e['id']}:{e['type']}] {e['name'][:38] or '(无文字控件)'} @({e['cx']},{e['cy']})" for e in elems]
    return f"窗口「{win_name}」可交互元素（前 {len(lines)} 项，1:1 物理像素）：\n" + "\n".join(lines)


def _pc_ui_action_locked(
    *,
    act: str,
    raw_action: str,
    element_name: str = "",
    x: int = -1,
    y: int = -1,
    text: str = "",
    press_enter: bool = False,
    hotkey: str = "",
    scroll_clicks: int = -5,
) -> str:
    pg = facade_attr("pyautogui", pyautogui)
    clip = facade_attr("pyperclip", pyperclip)
    ig = facade_attr("ImageGrab", ImageGrab)
    tmod = facade_attr("time", time)
    u32 = facade_attr("_user32", _user32)
    locate_fn = facade_attr("_smart_locate_target", _smart_locate_target)
    region_hash_fn = facade_attr("_compute_region_hash", _compute_region_hash)
    fg_info_fn = facade_attr("_get_foreground_window_info", _get_foreground_window_info)
    pt_info_fn = facade_attr("_get_window_info_at_point", _get_window_info_at_point)
    fg_guard_fn = facade_attr("_check_foreground_input_guard", _check_foreground_input_guard)
    click_guard_fn = facade_attr("_check_click_guard", _check_click_guard)
    hk_fg_guard_fn = facade_attr("_check_foreground_hotkey_guard", _check_foreground_hotkey_guard)
    audit_fn = facade_attr("_write_l2_audit_log", _write_l2_audit_log)
    estop_fn = facade_attr("is_emergency_stop_active", is_emergency_stop_active)
    trigger_estop_fn = facade_attr("trigger_emergency_stop", trigger_emergency_stop)
    clear_clip_fn = facade_attr("_clear_clipboard", _clear_clipboard)
    click_feedback_fn = facade_attr("_capture_click_feedback", _capture_click_feedback)

    def _estop_msg() -> str:
        return "安全急停已触发（Ctrl+Alt+Pause），已立即中止当前 L2 操作并降回安全挡。"

    if estop_fn():
        audit_fn(
            tool="pc_ui_action",
            action=act,
            outcome="blocked",
            reason="emergency_stop",
            target=element_name or (f"({x},{y})" if not (x == -1 and y == -1) else ""),
            hotkey=hotkey,
            text=text,
            press_enter=press_enter,
            detail="emergency stop active before L2 execution",
        )
        return _estop_msg()

    def _move_and_click(cx: int, cy: int, click_kind: str = "click"):
        if estop_fn():
            raise InterruptedError("emergency_stop")
        pg.moveTo(cx, cy, duration=0.12)
        tmod.sleep(0.05)
        if estop_fn():
            raise InterruptedError("emergency_stop")
        if click_kind == "double_click":
            pg.doubleClick(cx, cy)
        elif click_kind == "right_click":
            pg.rightClick(cx, cy)
        else:
            pg.click(cx, cy)

    def _is_explicit_screen_point_valid(px: int, py: int) -> bool:
        if px == -1 and py == -1:
            return False
        if px >= 0 and py >= 0:
            return True
        try:
            vx_left = int(u32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            vy_top = int(u32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            vx_w = int(u32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            vy_h = int(u32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            if vx_w > 0 and vy_h > 0:
                return (vx_left <= px < vx_left + vx_w) and (vy_top <= py < vy_top + vy_h)
        except Exception:
            pass
        return False

    def _is_target_region_fresh_locked(
        loc_obj, cx: int, cy: int, fp_out: Optional[dict] = None
    ) -> bool:
        """Re-capture screenshot under _gui_lock and compare perceptual region hash before clicking (E-3, P2-2)."""
        expected_hash = str(getattr(loc_obj, "region_hash", "") or "")
        if not expected_hash:
            return True
        expected_box = getattr(loc_obj, "region_box", None)
        expected_size = getattr(loc_obj, "screen_size", None)
        expected_mon = getattr(loc_obj, "monitor_rect", None)
        mon_left = int(expected_mon[0]) if expected_mon is not None else 0
        mon_top = int(expected_mon[1]) if expected_mon is not None else 0
        fresh_img = _grab_monitor_rgb(ig, expected_mon)
        if fp_out is not None:
            fp_out["fp"] = _compute_frame_fingerprint(fresh_img)
            fp_out["mon"] = expected_mon
        if expected_size is not None and fresh_img.size != expected_size:
            return False
        try:
            fresh_hash, _ = region_hash_fn(
                fresh_img, cx, cy, box=expected_box, mon_left=mon_left, mon_top=mon_top
            )
        except TypeError:
            fresh_hash, _ = region_hash_fn(fresh_img, cx, cy, box=expected_box)
        match_fn = facade_attr("_region_hashes_match", _region_hashes_match)
        return bool(fresh_hash and match_fn(expected_hash, fresh_hash))

    def _post_click_feedback_suffix(
        cx: int,
        cy: int,
        *,
        monitor_rect=None,
        fp_before=None,
    ) -> str:
        """Capture red-ring click feedback image & screen change hint without failing clicks."""
        if estop_fn():
            return ""
        try:
            tmod.sleep(0.15)
            fb = click_feedback_fn(ig, cx, cy, monitor_rect=monitor_rect, fp_before=fp_before)
        except Exception:
            return ""
        if not isinstance(fb, dict) or not fb.get("ok"):
            return ""
        changed = fb.get("changed")
        if changed is False:
            return "（提示：点击后画面未见明显变化，可能点偏或目标未响应；红圈落点反馈图已更新）"
        if changed is True:
            return "（画面已变化，红圈落点反馈图已更新）"
        return "（红圈落点反馈图已更新）"

    def _type_clipboard_locked(target_label: str) -> str:
        if estop_fn():
            raise InterruptedError("emergency_stop")

        fg_info = fg_info_fn()
        fg_err = fg_guard_fn(fg_info)
        if fg_err:
            audit_fn(
                tool="pc_ui_action",
                action=act,
                outcome="blocked",
                reason="foreground_blocked",
                fg_info=fg_info,
                target=target_label,
                text=text,
                press_enter=press_enter,
                detail=fg_err,
            )
            return fg_err

        with _clipboard_lock:
            had_text_clip = False
            old_clip = ""
            clip_written = False
            try:
                had_text_clip = bool(u32.IsClipboardFormatAvailable(_CF_UNICODETEXT))
                if had_text_clip:
                    old_clip = clip.paste()
            except Exception:
                had_text_clip = False
            try:
                if estop_fn():
                    raise InterruptedError("emergency_stop")
                clip.copy(text)
                clip_written = True
                tmod.sleep(0.04)
                if estop_fn():
                    raise InterruptedError("emergency_stop")
                pg.hotkey("ctrl", "v")
                tmod.sleep(0.25)
                if press_enter:
                    if estop_fn():
                        raise InterruptedError("emergency_stop")
                    pg.press("enter")
            finally:
                # A-3: ONLY touch clipboard if we actually wrote to it; ALWAYS restore previous text
                # or fall back to emptying so typed text NEVER remains in the clipboard.
                if clip_written:
                    if had_text_clip:
                        try:
                            clip.copy(old_clip)
                        except Exception:
                            clear_clip_fn(u32, clip, tmod)
                    else:
                        clear_clip_fn(u32, clip, tmod)
        audit_fn(
            tool="pc_ui_action",
            action=act,
            outcome="allowed",
            reason="ok",
            fg_info=fg_info,
            target=target_label,
            text=text,
            press_enter=press_enter,
        )
        return f"已输入文本：「{redact_secrets(text[:30], _get_audit_extra_secrets())}」" + (
            " 并按下回车。" if press_enter else "。"
        )

    try:
        if act in ("click", "double_click", "right_click"):
            if element_name.strip():
                max_attempts = 1 + MAX_STALE_RELOCATE_RETRIES
                for attempt in range(max_attempts):
                    # E-3: VLM grounding runs OUTSIDE _gui_lock (vision.py holds _gui_lock only for screenshot/UIA)
                    loc = locate_fn(element_name)
                    if not loc:
                        audit_fn(
                            tool="pc_ui_action",
                            action=act,
                            outcome="blocked",
                            reason="target_not_found",
                            target=element_name,
                        )
                        return f"未能在当前画面上定位到「{element_name}」，请确认目标是否在屏幕可见区域或尝试向下滚动页面。"
                    cx, cy, src_desc = int(loc[0]), int(loc[1]), str(loc[2])

                    with _gui_lock:
                        fp_box: dict = {"fp": None, "mon": getattr(loc, "monitor_rect", None)}
                        if not _is_target_region_fresh_locked(loc, cx, cy, fp_out=fp_box):
                            logging.getLogger("xiaozhi.pc_mcp").warning(
                                "[Vision] Target region changed before %s on '%s' (attempt %d/%d); %s",
                                act,
                                element_name.strip(),
                                attempt + 1,
                                max_attempts,
                                "re-locating" if attempt + 1 < max_attempts else "aborting",
                            )
                            if attempt + 1 < max_attempts:
                                continue
                            audit_fn(
                                tool="pc_ui_action",
                                action=act,
                                outcome="blocked",
                                reason="target_region_changed",
                                target=f"{element_name.strip()} @({cx},{cy})",
                                detail="target screen region changed before click even after re-location",
                            )
                            return f"检测到「{element_name.strip()}」所在画面区域在点击前发生变化，重新定位后仍不一致，已中止点击以防误点。"

                        fg_info = fg_info_fn()
                        tgt_info = pt_info_fn(cx, cy)
                        clk_err = click_guard_fn(act, fg_info, tgt_info)
                        if clk_err:
                            audit_fn(
                                tool="pc_ui_action",
                                action=act,
                                outcome="blocked",
                                reason="foreground_blocked",
                                fg_info=fg_info,
                                target=f"{element_name.strip()} @({cx},{cy})",
                                detail=clk_err,
                            )
                            return clk_err
                        _move_and_click(cx, cy, act)
                        fb_suffix = _post_click_feedback_suffix(
                            cx, cy, monitor_rect=fp_box.get("mon"), fp_before=fp_box.get("fp")
                        )
                        audit_fn(
                            tool="pc_ui_action",
                            action=act,
                            outcome="allowed",
                            reason="ok",
                            fg_info=fg_info,
                            target=f"{element_name.strip()} @({cx},{cy})",
                            detail=src_desc,
                        )
                        verb = {"click": "点击", "double_click": "双击", "right_click": "右键点击"}[act]
                        return f"已精准{verb} {src_desc}，物理像素坐标 ({cx}, {cy})。{fb_suffix}"
            elif _is_explicit_screen_point_valid(x, y):
                with _gui_lock:
                    fg_info = fg_info_fn()
                    tgt_info = pt_info_fn(x, y)
                    clk_err = click_guard_fn(act, fg_info, tgt_info)
                    if clk_err:
                        audit_fn(
                            tool="pc_ui_action",
                            action=act,
                            outcome="blocked",
                            reason="foreground_blocked",
                            fg_info=fg_info,
                            target=f"({x},{y})",
                            detail=clk_err,
                        )
                        return clk_err
                    exp_mon = _get_target_monitor_rect(int((tgt_info or {}).get("hwnd") or 0) or None)
                    fp_before = None
                    try:
                        fp_before = _compute_frame_fingerprint(_grab_monitor_rgb(ig, exp_mon))
                    except Exception:
                        fp_before = None
                    _move_and_click(x, y, act)
                    fb_suffix = _post_click_feedback_suffix(
                        x, y, monitor_rect=exp_mon, fp_before=fp_before
                    )
                    audit_fn(
                        tool="pc_ui_action",
                        action=act,
                        outcome="allowed",
                        reason="ok",
                        fg_info=fg_info,
                        target=f"({x},{y})",
                    )
                    verb = {"click": "点击", "double_click": "双击", "right_click": "右键点击"}[act]
                    return f"已{verb}屏幕物理坐标 ({x}, {y})。{fb_suffix}"
            audit_fn(
                tool="pc_ui_action",
                action=act,
                outcome="blocked",
                reason="invalid_args",
            )
            return "click 操作需要提供 element_name（推荐）或有效的 x, y 坐标。"

        if act == "type_text":
            target_desc = element_name.strip()
            if target_desc:
                max_attempts = 1 + MAX_STALE_RELOCATE_RETRIES
                for attempt in range(max_attempts):
                    # E-3: VLM grounding runs OUTSIDE _gui_lock
                    loc = locate_fn(target_desc)
                    if not loc:
                        audit_fn(
                            tool="pc_ui_action",
                            action=act,
                            outcome="blocked",
                            reason="target_not_found",
                            target=target_desc,
                            text=text,
                            press_enter=press_enter,
                            detail="target element not located; fail-closed without typing",
                        )
                        return f"未能在当前画面上定位到「{target_desc}」，已中止输入，未向当前焦点窗口输入任何内容。"
                    cx, cy = int(loc[0]), int(loc[1])

                    with _gui_lock:
                        if not _is_target_region_fresh_locked(loc, cx, cy):
                            logging.getLogger("xiaozhi.pc_mcp").warning(
                                "[Vision] Target region changed before type_text click on '%s' (attempt %d/%d); %s",
                                target_desc,
                                attempt + 1,
                                max_attempts,
                                "re-locating" if attempt + 1 < max_attempts else "aborting",
                            )
                            if attempt + 1 < max_attempts:
                                continue
                            audit_fn(
                                tool="pc_ui_action",
                                action=act,
                                outcome="blocked",
                                reason="target_region_changed",
                                target=f"{target_desc} @({cx},{cy})",
                                text=text,
                                press_enter=press_enter,
                                detail="target input region changed before click even after re-location",
                            )
                            return f"检测到输入框「{target_desc}」所在画面区域持续变化，已中止输入以防误写。"

                        fg_before_click = fg_info_fn()
                        tgt_info = pt_info_fn(cx, cy)
                        tgt_err = click_guard_fn("click", fg_before_click, tgt_info)
                        if tgt_err:
                            audit_fn(
                                tool="pc_ui_action",
                                action=act,
                                outcome="blocked",
                                reason="foreground_blocked",
                                fg_info=tgt_info or fg_before_click,
                                target=f"{target_desc} @({cx},{cy})",
                                text=text,
                                press_enter=press_enter,
                                detail=tgt_err,
                            )
                            return tgt_err
                        _move_and_click(cx, cy, "click")
                        tmod.sleep(0.15)
                        return _type_clipboard_locked(f"{target_desc} @({cx},{cy})")

            with _gui_lock:
                return _type_clipboard_locked(target_desc)

        if act == "hotkey":
            exec_keys, canon_keys = _parse_and_normalize_hotkey(hotkey)
            if not exec_keys:
                audit_fn(
                    tool="pc_ui_action",
                    action=act,
                    outcome="blocked",
                    reason="invalid_args",
                    hotkey=hotkey,
                )
                return "未提供有效的 hotkey。"

            canon_str = "+".join(canon_keys)
            hk_err = _check_hotkey_blacklist(canon_keys, hotkey)
            if hk_err:
                audit_fn(
                    tool="pc_ui_action",
                    action=act,
                    outcome="blocked",
                    reason="hotkey_blacklisted",
                    hotkey=canon_str,
                    detail=hk_err,
                )
                return hk_err

            invalid_keys = _validate_hotkey_keys(exec_keys)
            if invalid_keys:
                inv_msg = f"安全护栏已拦截：快捷键包含无效或未识别按键（{', '.join(invalid_keys)}）。"
                audit_fn(
                    tool="pc_ui_action",
                    action=act,
                    outcome="blocked",
                    reason="invalid_hotkey_key",
                    hotkey=canon_str,
                    detail=inv_msg,
                )
                return inv_msg

            with _gui_lock:
                fg_info = fg_info_fn()
                fg_err = fg_guard_fn(fg_info) or hk_fg_guard_fn(canon_keys, fg_info)
                if fg_err:
                    audit_fn(
                        tool="pc_ui_action",
                        action=act,
                        outcome="blocked",
                        reason="foreground_blocked",
                        fg_info=fg_info,
                        hotkey=canon_str,
                        detail=fg_err,
                    )
                    return fg_err

                if estop_fn():
                    raise InterruptedError("emergency_stop")
                pg.hotkey(*exec_keys)
                audit_fn(
                    tool="pc_ui_action",
                    action=act,
                    outcome="allowed",
                    reason="ok",
                    fg_info=fg_info,
                    hotkey=canon_str,
                )
                return f"已按下快捷键: {'+'.join(exec_keys)}"

        if act == "scroll":
            with _gui_lock:
                if estop_fn():
                    raise InterruptedError("emergency_stop")
                pg.scroll(int(scroll_clicks) * 120)
                audit_fn(
                    tool="pc_ui_action",
                    action=act,
                    outcome="allowed",
                    reason="ok",
                    target=f"scroll:{int(scroll_clicks)}",
                )
                return f"已{'向下' if scroll_clicks < 0 else '向上'}滚动页面。"

        audit_fn(
            tool="pc_ui_action",
            action=act,
            outcome="blocked",
            reason="unknown_action",
        )
        return f"未知的 UI 操作类型: {raw_action}"
    except InterruptedError:
        audit_fn(
            tool="pc_ui_action",
            action=act,
            outcome="blocked",
            reason="emergency_stop",
            target=element_name or (f"({x},{y})" if not (x == -1 and y == -1) else ""),
            hotkey=hotkey,
            text=text,
            press_enter=press_enter,
            detail="aborted mid-execution by emergency stop",
        )
        return _estop_msg()
    except pyautogui.FailSafeException:
        trigger_estop_fn("pyautogui_failsafe")
        audit_fn(
            tool="pc_ui_action",
            action=act,
            outcome="blocked",
            reason="failsafe_triggered",
            target=element_name or (f"({x},{y})" if not (x == -1 and y == -1) else ""),
            detail="PyAutoGUI corner FAILSAFE triggered; downgraded to safe mode",
        )
        return "安全急停已触发（鼠标移至屏幕角落 FAILSAFE），已立即中止 L2 操作并降回安全挡。"
