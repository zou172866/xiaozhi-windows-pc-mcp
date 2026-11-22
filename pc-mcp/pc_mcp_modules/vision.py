"""Multimodal screen vision (DeepSeek/Gemini), 18-level UIA control walk, SoM overlay & 3-stage grounding (P2-20, E-3)."""

from __future__ import annotations

import atexit
import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageGrab
import pyautogui

from . import (
    CONFIG_YAML as _DEFAULT_CONFIG_YAML,
    LEGACY_CONFIG_YAML as _LEGACY_CONFIG_YAML,
    LAST_GROUNDING_IMG as _DEFAULT_LAST_GROUNDING_IMG,
    _com_mta_scope,
    _get_env_key,
    _gui_lock,
    facade_attr,
)

_POSITIONAL_HINTS = (
    "第", "首个", "最后", "左上", "右上", "左下", "右下", "中间", "正中",
    "左边", "右边", "上方", "下方", "封面", "缩略图", "图标", "头像", "卡片", "视频",
)

_vision_http_lock = threading.Lock()
_vision_http_client: Optional[httpx.Client] = None
_vision_proxy_http_client: Optional[httpx.Client] = None

_HTTP_LIMITS = httpx.Limits(
    max_connections=10,
    max_keepalive_connections=4,
    keepalive_expiry=60.0,
)


def _get_vision_http_client(use_proxy: bool = False) -> httpx.Client:
    """Lazily initialize persistent thread-safe httpx.Client instances (E-3, P0-1).

    - `use_proxy=False` (default, used for domestic DeepSeek VLM): `trust_env=False` so
      domestic requests never route through overseas HTTP/SOCKS proxies or crash without `socksio`.
    - `use_proxy=True` (used for Gemini VLM): `trust_env=True` with automatic fallback to
      direct `trust_env=False` if `socksio` is unavailable in the Python environment.
    """
    global _vision_http_client, _vision_proxy_http_client
    with _vision_http_lock:
        if not use_proxy:
            if _vision_http_client is None or _vision_http_client.is_closed:
                _vision_http_client = httpx.Client(
                    timeout=16.0,
                    limits=_HTTP_LIMITS,
                    trust_env=False,
                )
            return _vision_http_client
        if _vision_proxy_http_client is None or _vision_proxy_http_client.is_closed:
            try:
                _vision_proxy_http_client = httpx.Client(
                    timeout=16.0,
                    limits=_HTTP_LIMITS,
                    trust_env=True,
                )
            except ImportError:
                _vision_proxy_http_client = httpx.Client(
                    timeout=16.0,
                    limits=_HTTP_LIMITS,
                    trust_env=False,
                )
        return _vision_proxy_http_client


def close_vision_http_client() -> None:
    """Close shared persistent vision httpx.Client instances cleanly on shutdown."""
    global _vision_http_client, _vision_proxy_http_client
    with _vision_http_lock:
        for cli in (_vision_http_client, _vision_proxy_http_client):
            if cli is not None:
                try:
                    cli.close()
                except Exception:
                    pass
        _vision_http_client = None
        _vision_proxy_http_client = None


from .winapi import _user32 as _win_user32


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _get_target_monitor_rect(hwnd: Optional[int] = None) -> Optional[tuple[int, int, int, int]]:
    """Query physical bounding rectangle `(left, top, right, bottom)` of the monitor hosting `hwnd` (P2-2)."""
    try:
        u32 = _win_user32
        target_hwnd = int(hwnd or 0) or int(u32.GetForegroundWindow() or 0)
        if not target_hwnd:
            return None
        hmon = u32.MonitorFromWindow(wintypes.HWND(target_hwnd), 2)  # MONITOR_DEFAULTTONEAREST = 2
        if not hmon:
            return None
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not u32.GetMonitorInfoW(wintypes.HMONITOR(hmon), ctypes.byref(mi)):
            return None
        left, top, right, bottom = (
            int(mi.rcMonitor.left),
            int(mi.rcMonitor.top),
            int(mi.rcMonitor.right),
            int(mi.rcMonitor.bottom),
        )
        if right > left and bottom > top:
            return (left, top, right, bottom)
    except Exception:
        pass
    return None


def _grab_monitor_rgb(
    ig: Any, monitor_rect: Optional[tuple[int, int, int, int]] = None
) -> Image.Image:
    """Grab an RGB screenshot of `monitor_rect`, preserving zero-arg `ig.grab()` on the primary screen (P2-2)."""
    if monitor_rect is not None:
        left, top, right, bottom = monitor_rect
        is_primary = left == 0 and top == 0
        if is_primary:
            try:
                pg = facade_attr("pyautogui", pyautogui)
                psw, psh = pg.size()
                if (right - left, bottom - top) != (int(psw), int(psh)):
                    is_primary = False
            except Exception:
                pass
        if not is_primary:
            try:
                return ig.grab(bbox=monitor_rect, all_screens=True).convert("RGB")
            except TypeError:
                pass
    return ig.grab().convert("RGB")


atexit.register(close_vision_http_client)


class LocateResult(tuple):
    """Backward-compatible 3-tuple `(x, y, src_tag)` carrying target-region staleness metadata (E-3)."""

    def __new__(
        cls,
        x: int,
        y: int,
        src_tag: str,
        *,
        region_hash: str = "",
        region_box: Optional[tuple[int, int, int, int]] = None,
        screen_size: Optional[tuple[int, int]] = None,
        monitor_rect: Optional[tuple[int, int, int, int]] = None,
    ):
        obj = super().__new__(cls, (int(x), int(y), str(src_tag)))
        obj.region_hash = str(region_hash or "")
        obj.region_box = region_box
        obj.screen_size = screen_size
        obj.monitor_rect = monitor_rect
        return obj


_MARK_ID_EXACT_RE = re.compile(r"^[\s\[\(\{【<#]*([+-]?\d+)(?:\.0+)?[\s\]\)\}】>]*$")
_MARK_ID_LABELED_RE = re.compile(
    r"(?:^|[\s,(（\[【])(?:som|mark(?:_id)?|id|编号|#)\s*[:=#_-]?\s*([+-]?\d+)(?:\.0+)?(?=$|[\s,)）\]】])",
    re.IGNORECASE,
)


def _parse_mark_id(raw: Any, max_id: int = 0) -> int:
    """Fault-tolerant parser for VLM `mark_id` (handles strings, whitespace, brackets, prefixes, out-of-range) (E-3)."""
    if raw is None or isinstance(raw, bool):
        return 0
    val: Optional[int] = None
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        if math.isfinite(raw) and raw.is_integer():
            val = int(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if s:
            m = _MARK_ID_EXACT_RE.fullmatch(s)
            if m:
                try:
                    val = int(m.group(1))
                except ValueError:
                    val = None
            else:
                m2 = _MARK_ID_LABELED_RE.search(s)
                if m2:
                    try:
                        val = int(m2.group(1))
                    except ValueError:
                        val = None
    if val is None or val < 1:
        return 0
    if max_id > 0 and val > max_id:
        return 0
    return val


def _parse_coord_float(raw: Any, default: float = -1.0) -> float:
    """Fault-tolerant parser for normalized coordinate numbers from VLM JSON."""
    if raw is None or isinstance(raw, bool):
        return default
    try:
        v = float(raw)
        if not math.isfinite(v):
            return default
        return v
    except (TypeError, ValueError, OverflowError):
        return default


def _compute_region_hash(
    img: Image.Image,
    cx: int,
    cy: int,
    *,
    box: Optional[tuple[int, int, int, int]] = None,
    half_w: int = 48,
    half_h: int = 32,
    mon_left: int = 0,
    mon_top: int = 0,
) -> tuple[str, tuple[int, int, int, int]]:
    """Compute a blinking-cursor-tolerant perceptual hash (mean RGB + 64-bit dHash) around `(cx, cy)` (E-3, P2-2)."""
    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        return "", (0, 0, 0, 0)
    if box is not None:
        x1, y1, x2, y2 = box
    else:
        local_cx = int(cx) - int(mon_left)
        local_cy = int(cy) - int(mon_top)
        x1 = local_cx - half_w
        y1 = local_cy - half_h
        x2 = local_cx + half_w
        y2 = local_cy + half_h
    x1 = max(0, min(sw - 1, int(x1)))
    y1 = max(0, min(sh - 1, int(y1)))
    x2 = max(x1 + 1, min(sw, int(x2)))
    y2 = max(y1 + 1, min(sh, int(y2)))
    crop = img.crop((x1, y1, x2, y2)).convert("RGB")
    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR", 2)

    # Mean RGB color signature (catches solid-color / theme / popup transitions)
    rgb_small = crop.resize((8, 8), resample=resample)
    rgb_bytes = rgb_small.tobytes()
    n_px = max(1, len(rgb_bytes) // 3)
    r_mean = sum(rgb_bytes[0::3]) // n_px
    g_mean = sum(rgb_bytes[1::3]) // n_px
    b_mean = sum(rgb_bytes[2::3]) // n_px

    # 64-bit grayscale horizontal gradient dHash (tolerates 1-2px blinking carets / subpixel anti-aliasing)
    gray = crop.convert("L").resize((9, 8), resample=resample)
    g_bytes = gray.tobytes()
    bits = 0
    for row in range(8):
        off = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if g_bytes[off + col] > g_bytes[off + col + 1] else 0)

    digest = f"phash:{r_mean:02x}{g_mean:02x}{b_mean:02x}:{bits:016x}"
    return digest, (x1, y1, x2, y2)


def _region_hashes_match(
    expected_hash: str,
    fresh_hash: str,
    *,
    max_hamming: int = 4,
    max_rgb_delta: int = 12,
) -> bool:
    """Compare two region hashes with perceptual tolerance for blinking cursors while catching layout/popup shifts (4.1)."""
    if not expected_hash or not fresh_hash:
        return False
    if expected_hash == fresh_hash:
        return True
    if expected_hash.startswith("phash:") and fresh_hash.startswith("phash:"):
        p1 = expected_hash.split(":")
        p2 = fresh_hash.split(":")
        if len(p1) == 3 and len(p2) == 3 and len(p1[1]) == 6 and len(p2[1]) == 6:
            try:
                r1, g1, b1 = int(p1[1][0:2], 16), int(p1[1][2:4], 16), int(p1[1][4:6], 16)
                r2, g2, b2 = int(p2[1][0:2], 16), int(p2[1][2:4], 16), int(p2[1][4:6], 16)
                if max(abs(r1 - r2), abs(g1 - g2), abs(b1 - b2)) > max_rgb_delta:
                    return False
                hamming = (int(p1[2], 16) ^ int(p2[2], 16)).bit_count()
                return hamming <= max_hamming
            except ValueError:
                return False
    return False


def _prefer_gemini_vision() -> bool:
    cfg_yaml = Path(facade_attr("CONFIG_YAML", _DEFAULT_CONFIG_YAML))
    candidates = [cfg_yaml]
    if cfg_yaml == _DEFAULT_CONFIG_YAML:
        candidates.append(_LEGACY_CONFIG_YAML)
    for cand in candidates:
        try:
            if cand.exists():
                txt = cand.read_text(encoding="utf-8", errors="ignore")
                if re.search(r"^\s*LLM:\s*GeminiOpenAI\b", txt, re.MULTILINE):
                    return True
                return False
        except Exception:
            pass
    return False


def _call_vision_llm(prompt_text: str, pil_img: Image.Image, json_mode: bool = False) -> Optional[str]:
    """统一多模态视觉调用：默认优先走国内低延迟 DeepSeek V4.1 Flash 原生视觉，自动互备 Gemini 3.5 Flash-Lite。"""
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    ds_key = _get_env_key("DEEPSEEK_API_KEY")
    gem_key = _get_env_key("GEMINI_API_KEY")

    ds_entry = (
        "deepseek",
        "https://api.deepseek.com/chat/completions",
        ds_key,
        "deepseek-flash",
        {"thinking": {"type": "disabled"}},
    )
    gem_entry = (
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        gem_key,
        "gemini-3.5-flash-lite",
        {},
    )
    providers = [gem_entry, ds_entry] if _prefer_gemini_vision() else [ds_entry, gem_entry]

    for _name, url, api_key, model_name, extra_body in providers:
        if not api_key:
            continue
        payload = {
            "model": model_name,
            "temperature": 0.0 if json_mode else 0.2,
            **extra_body,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            client = _get_vision_http_client() if _name == "deepseek" else _get_vision_http_client(True)
            r = client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=16.0,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content:
                    return str(content).strip()
        except Exception:
            continue
    return None


def _parse_json_obj(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def _walk_uia_controls(
    auto, root, max_items: int, sw: int, sh: int, *, mon_left: int = 0, mon_top: int = 0
) -> list[dict]:
    items: list[dict] = []
    interesting_types = {
        "HyperlinkControl",
        "ButtonControl",
        "EditControl",
        "TabItemControl",
        "MenuItemControl",
        "ListItemControl",
        "CheckBoxControl",
        "ComboBoxControl",
    }
    if not root:
        return items
    visited = 0
    for ctrl, _depth in auto.WalkControl(root, includeTop=False, maxDepth=18):
        visited += 1
        if visited > 2800 or len(items) >= max_items:
            break
        try:
            ctype = ctrl.ControlTypeName
            if ctype not in interesting_types:
                continue
            rect = ctrl.BoundingRectangle
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 12 or h < 12 or w > sw * 0.92 or h > sh * 0.85:
                continue
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            if (
                cx < mon_left + 4
                or cy < mon_top + 4
                or cx > mon_left + sw - 4
                or cy > mon_top + sh - 4
            ):
                continue
            cname = (ctrl.Name or "").strip()
            dup = False
            for ex in items:
                if abs(ex["cx"] - cx) <= 10 and abs(ex["cy"] - cy) <= 10:
                    if not ex["name"] and cname:
                        ex["name"] = cname
                    dup = True
                    break
            if dup:
                continue
            items.append(
                {
                    "id": len(items) + 1,
                    "type": ctype.replace("Control", ""),
                    "name": cname,
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "right": int(rect.right),
                    "bottom": int(rect.bottom),
                    "cx": int(cx),
                    "cy": int(cy),
                }
            )
        except Exception:
            continue
    return items


def _collect_uia_elements(
    target_win=None,
    max_items: int = 70,
    window_keyword: str = "",
    *,
    monitor_rect: Optional[tuple[int, int, int, int]] = None,
) -> list[dict]:
    """穿透 18 层（支持 Edge/Chrome 网页 DOM DocumentControl）采集当前可见可交互控件及其 1:1 物理像素矩形。"""
    import uiautomation as auto

    pg = facade_attr("pyautogui", pyautogui)
    sw, sh = pg.size()
    with _com_mta_scope():
        root = target_win
        if root is None and window_keyword.strip():
            kw_low = window_keyword.strip().lower()
            for w in auto.GetRootControl().GetChildren():
                if kw_low in (w.Name or "").lower():
                    root = w
                    break
        if root is None:
            root = auto.GetForegroundControl()
        rect = monitor_rect
        if rect is None and root is not None:
            rect = _get_target_monitor_rect(getattr(root, "NativeWindowHandle", None))
        if rect is not None:
            mon_left, mon_top, mon_right, mon_bottom = rect
            return _walk_uia_controls(
                auto,
                root,
                max_items,
                mon_right - mon_left,
                mon_bottom - mon_top,
                mon_left=mon_left,
                mon_top=mon_top,
            )
        return _walk_uia_controls(auto, root, max_items, sw, sh)


def _draw_som_overlay(
    base_img: Image.Image, elements: list[dict], *, mon_left: int = 0, mon_top: int = 0
) -> Image.Image:
    """在截图上绘制 Set-of-Mark 红色编号角标框，供多模态视觉模型做零误差数字锚定。"""
    annotated = base_img.copy()
    dr = ImageDraw.Draw(annotated)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()

    for el in elements[:60]:
        idx_str = str(el["id"])
        x1 = int(el["left"]) - int(mon_left)
        y1 = int(el["top"]) - int(mon_top)
        x2 = int(el["right"]) - int(mon_left)
        y2 = int(el["bottom"]) - int(mon_top)
        dr.rectangle([x1, y1, x2, y2], outline=(220, 38, 38), width=2)
        tw = max(22, 11 * len(idx_str) + 8)
        th = 22
        bx1 = max(0, min(annotated.width - tw, x1))
        by1 = y1 - th if y1 >= th else max(0, y1)
        dr.rectangle([bx1, by1, bx1 + tw, by1 + th], fill=(220, 38, 38))
        dr.text((bx1 + 4, by1 + 2), idx_str, fill=(255, 255, 255), font=font)
    return annotated


def _smart_locate_target(target_desc: str) -> Optional[LocateResult]:
    """三阶高精度鼠标定位引擎（UIA 直点 -> Set-of-Mark 视觉定位 -> 1:1 局部高清裁剪二次精修）。

    E-3: Acquires `_gui_lock` ONLY for UIA collection + screenshot capture, releasing it
    immediately before any VLM inference so `_gui_lock` is never held across network I/O.
    Returns `LocateResult` (backward-compatible 3-tuple with `region_hash` / `region_box`).
    """
    kw = target_desc.strip()
    kw_low = kw.lower()
    needs_visual_layout = any(h in kw for h in _POSITIONAL_HINTS)

    collect_fn = facade_attr("_collect_uia_elements", _collect_uia_elements)
    call_vlm = facade_attr("_call_vision_llm", _call_vision_llm)
    ig = facade_attr("ImageGrab", ImageGrab)
    grounding_img = Path(facade_attr("LAST_GROUNDING_IMG", _DEFAULT_LAST_GROUNDING_IMG))

    # Critical section: collect UIA tree and grab screenshot atomically, then release _gui_lock IMMEDIATELY!
    with _gui_lock:
        mon_rect = _get_target_monitor_rect()
        mon_left = int(mon_rect[0]) if mon_rect is not None else 0
        mon_top = int(mon_rect[1]) if mon_rect is not None else 0
        try:
            uia_elems = collect_fn(max_items=65, monitor_rect=mon_rect)
        except TypeError:
            uia_elems = collect_fn(max_items=65)

        # 第一阶：非方位词描述时，尝试 UIA 精确或强子串匹配
        if not needs_visual_layout and uia_elems:
            exact_hit = None
            partial_hit = None
            for el in uia_elems:
                nm = el["name"].strip().lower()
                if not nm:
                    continue
                if nm == kw_low:
                    exact_hit = el
                    break
                if len(kw_low) >= 2 and kw_low in nm and partial_hit is None:
                    partial_hit = el
            hit = exact_hit or partial_hit
            if hit:
                # 4.1 #3: Capture region hash on Stage-1 UIA hit too so pre-click freshness check runs after unlock.
                uia_hash = ""
                uia_box = None
                uia_screen_size = None
                try:
                    uia_img = _grab_monitor_rgb(ig, mon_rect)
                    uia_hash, uia_box = _compute_region_hash(
                        uia_img, hit["cx"], hit["cy"], mon_left=mon_left, mon_top=mon_top
                    )
                    uia_screen_size = uia_img.size
                except Exception:
                    pass
                return LocateResult(
                    hit["cx"],
                    hit["cy"],
                    f"UIA控件「{hit['name'][:30]}」",
                    region_hash=uia_hash,
                    region_box=uia_box,
                    screen_size=uia_screen_size,
                    monitor_rect=mon_rect,
                )

        # 第二阶截图采样在锁内完成，随后立刻释放 _gui_lock，避免 VLM 推理占锁长达 64s
        raw_img = _grab_monitor_rgb(ig, mon_rect)

    # _gui_lock 已释放：以下 SoM 绘制、VLM 粗定位与高清裁剪二次精修均在锁外执行
    sw, sh = raw_img.size
    marked_subset = uia_elems[:55]
    som_img = (
        _draw_som_overlay(raw_img, marked_subset, mon_left=mon_left, mon_top=mon_top)
        if marked_subset
        else raw_img
    )

    send_w = min(1600, sw)
    send_h = int(sh * (send_w / sw))
    send_img = som_img.resize((send_w, send_h)) if send_w != sw else som_img

    legend_lines = [
        f"[{el['id']}] {el['type']}: {el['name'][:28]}"
        for el in marked_subset
        if el["name"]
    ][:35]
    legend_text = ("\n已知部分编号控件文字参考：\n" + "\n".join(legend_lines)) if legend_lines else ""

    prompt_coarse = (
        f"请在这张电脑屏幕截图中精确定位用户想要操作的目标：「{kw}」。\n"
        "图中部分可交互元素左上角标有红底白字编号（如 1, 2, 3...）。\n"
        "请返回严格 JSON 对象（不要输出任何额外解释）：\n"
        "{\n"
        '  "found": true,\n'
        '  "mark_id": 0,\n'
        '  "label": "识别到的目标简短名称",\n'
        '  "x_min": 120,\n'
        '  "y_min": 240,\n'
        '  "x_max": 360,\n'
        '  "y_max": 420\n'
        "}\n"
        "字段说明：\n"
        "- found: 画面中是否存在该目标（true/false）；\n"
        "- mark_id: 若某个红底数字框恰好就是该目标，填其整数编号；若目标（如视频封面大图、自绘按钮）没有贴合的红框，填 0；\n"
        "- x_min, y_min, x_max, y_max: 目标可点击区域在整张图中的边界框，全部归一化到 0~1000（左边缘 x=0，右边缘 x=1000；上边缘 y=0，下边缘 y=1000）。"
        + legend_text
    )

    resp_obj = _parse_json_obj(call_vlm(prompt_coarse, send_img, json_mode=True))
    if not resp_obj or not resp_obj.get("found"):
        return None

    mark_id = _parse_mark_id(resp_obj.get("mark_id"), max_id=len(marked_subset))
    label = str(resp_obj.get("label") or kw)[:32]
    x_min = _parse_coord_float(resp_obj.get("x_min"), -1.0)
    y_min = _parse_coord_float(resp_obj.get("y_min"), -1.0)
    x_max = _parse_coord_float(resp_obj.get("x_max"), -1.0)
    y_max = _parse_coord_float(resp_obj.get("y_max"), -1.0)

    has_box = (0 <= x_min < x_max <= 1000) and (0 <= y_min < y_max <= 1000)
    vx = int(((x_min + x_max) / 2000.0) * sw) if has_box else -1
    vy = int(((y_min + y_max) / 2000.0) * sh) if has_box else -1
    vw = int(((x_max - x_min) / 1000.0) * sw) if has_box else 0
    vh = int(((y_max - y_min) / 1000.0) * sh) if has_box else 0

    local_x, local_y, src_tag = -1, -1, ""
    has_resolved = False

    if 1 <= mark_id <= len(marked_subset):
        el = marked_subset[mark_id - 1]
        el_lx = int(el["cx"]) - mon_left
        el_ly = int(el["cy"]) - mon_top
        if not has_box or (abs(el_lx - vx) <= max(200, vw) and abs(el_ly - vy) <= max(150, vh)):
            local_x, local_y = el_lx, el_ly
            has_resolved = True
            src_tag = f"SoM#{mark_id}「{el['name'][:24] or label}」"

    if not has_resolved and has_box:
        local_x, local_y = vx, vy
        has_resolved = True
        src_tag = f"视觉定位「{label}」"
        if vw < 200 or vh < 110:
            crop_w, crop_h = min(768, sw), min(512, sh)
            cx1 = max(0, min(sw - crop_w, vx - crop_w // 2))
            cy1 = max(0, min(sh - crop_h, vy - crop_h // 2))
            cx2, cy2 = cx1 + crop_w, cy1 + crop_h
            crop_img = raw_img.crop((cx1, cy1, cx2, cy2))
            prompt_fine = (
                f"这是从屏幕局部 1:1 放大裁剪出的高清区域，请精确定位其中「{kw}」的可点击中心框。\n"
                '返回严格 JSON：{"found": true, "x_min": 0~1000, "y_min": 0~1000, "x_max": 0~1000, "y_max": 0~1000}。'
            )
            fine_obj = _parse_json_obj(call_vlm(prompt_fine, crop_img, json_mode=True))
            if fine_obj and fine_obj.get("found"):
                fx1 = _parse_coord_float(fine_obj.get("x_min"), -1.0)
                fy1 = _parse_coord_float(fine_obj.get("y_min"), -1.0)
                fx2 = _parse_coord_float(fine_obj.get("x_max"), -1.0)
                fy2 = _parse_coord_float(fine_obj.get("y_max"), -1.0)
                if 0 <= fx1 < fx2 <= 1000 and 0 <= fy1 < fy2 <= 1000:
                    local_x = cx1 + int(((fx1 + fx2) / 2000.0) * crop_w)
                    local_y = cy1 + int(((fy1 + fy2) / 2000.0) * crop_h)
                    src_tag = f"高清精修定位「{label}」"

    if not has_resolved or local_x < 0 or local_y < 0:
        return None

    local_x = max(2, min(sw - 2, local_x))
    local_y = max(2, min(sh - 2, local_y))
    final_x = mon_left + local_x
    final_y = mon_top + local_y
    region_hash, region_box = _compute_region_hash(
        raw_img, final_x, final_y, mon_left=mon_left, mon_top=mon_top
    )

    try:
        dbg = som_img.copy()
        d = ImageDraw.Draw(dbg)
        d.ellipse([local_x - 16, local_y - 16, local_x + 16, local_y + 16], outline=(16, 185, 129), width=4)
        d.line([local_x - 26, local_y, local_x + 26, local_y], fill=(16, 185, 129), width=3)
        d.line([local_x, local_y - 26, local_x, local_y + 26], fill=(16, 185, 129), width=3)
        tmp_img = grounding_img.with_name(f".{grounding_img.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        dbg.resize((1280, max(1, int(sh * (1280 / sw))))).save(tmp_img, format="JPEG", quality=82)
        os.replace(tmp_img, grounding_img)
    except Exception:
        pass

    return LocateResult(
        final_x,
        final_y,
        src_tag,
        region_hash=region_hash,
        region_box=region_box,
        screen_size=(sw, sh),
        monitor_rect=mon_rect,
    )


def look_at_screen(question: str = "请简明描述当前电脑屏幕上正在显示的主要内容。") -> str:
    ig = facade_attr("ImageGrab", ImageGrab)
    call_vlm = facade_attr("_call_vision_llm", _call_vision_llm)
    with _gui_lock:
        img = _grab_monitor_rgb(ig, _get_target_monitor_rect())
    w, h = img.size
    if w > 1600:
        img = img.resize((1600, int(h * (1600 / w))))

    prompt_q = f"用户正在通过语音询问他当前的电脑屏幕：{question}\n请用清晰简洁的中文直接给出准确观察结果或解答（控制在120字以内，便于语音播报）。"
    ans = call_vlm(prompt_q, img, json_mode=False)
    if not ans:
        return "屏幕视觉识别接口暂不可用，请检查 DEEPSEEK_API_KEY 或 GEMINI_API_KEY。"
    return f"屏幕画面分析结果：{ans}"
