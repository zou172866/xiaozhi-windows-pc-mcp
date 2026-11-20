"""Multimodal screen vision (DeepSeek/Gemini), 18-level UIA control walk, SoM overlay & 3-stage grounding (P2-20)."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import re
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageGrab
import pyautogui

from . import (
    CONFIG_YAML as _DEFAULT_CONFIG_YAML,
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


def _prefer_gemini_vision() -> bool:
    cfg_yaml = Path(facade_attr("CONFIG_YAML", _DEFAULT_CONFIG_YAML))
    try:
        if cfg_yaml.exists():
            txt = cfg_yaml.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*LLM:\s*GeminiOpenAI\b", txt, re.MULTILINE):
                return True
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
            r = httpx.post(
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


def _walk_uia_controls(auto, root, max_items: int, sw: int, sh: int) -> list[dict]:
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
            if cx < 4 or cy < 4 or cx > sw - 4 or cy > sh - 4:
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
    target_win=None, max_items: int = 70, window_keyword: str = ""
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
        return _walk_uia_controls(auto, root, max_items, sw, sh)


def _draw_som_overlay(base_img: Image.Image, elements: list[dict]) -> Image.Image:
    """在截图上绘制 Set-of-Mark 红色编号角标框，供多模态视觉模型做零误差数字锚定。"""
    annotated = base_img.copy()
    dr = ImageDraw.Draw(annotated)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()

    for el in elements[:60]:
        idx_str = str(el["id"])
        x1, y1, x2, y2 = el["left"], el["top"], el["right"], el["bottom"]
        dr.rectangle([x1, y1, x2, y2], outline=(220, 38, 38), width=2)
        tw = max(22, 11 * len(idx_str) + 8)
        th = 22
        bx1 = max(0, min(annotated.width - tw, x1))
        by1 = y1 - th if y1 >= th else y1
        dr.rectangle([bx1, by1, bx1 + tw, by1 + th], fill=(220, 38, 38))
        dr.text((bx1 + 4, by1 + 2), idx_str, fill=(255, 255, 255), font=font)
    return annotated


def _smart_locate_target(target_desc: str) -> Optional[tuple[int, int, str]]:
    """三阶高精度鼠标定位引擎（UIA 直点 -> Set-of-Mark 视觉定位 -> 1:1 局部高清裁剪二次精修）。"""
    kw = target_desc.strip()
    kw_low = kw.lower()
    needs_visual_layout = any(h in kw for h in _POSITIONAL_HINTS)

    collect_fn = facade_attr("_collect_uia_elements", _collect_uia_elements)
    call_vlm = facade_attr("_call_vision_llm", _call_vision_llm)
    ig = facade_attr("ImageGrab", ImageGrab)
    grounding_img = Path(facade_attr("LAST_GROUNDING_IMG", _DEFAULT_LAST_GROUNDING_IMG))

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
            return hit["cx"], hit["cy"], f"UIA控件「{hit['name'][:30]}」"

    # 第二阶：1:1 物理全屏截图 + Set-of-Mark 角标 + 0~1000 归一化坐标定位
    raw_img = ig.grab().convert("RGB")
    sw, sh = raw_img.size
    marked_subset = uia_elems[:55]
    som_img = _draw_som_overlay(raw_img, marked_subset) if marked_subset else raw_img

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

    mark_id = int(resp_obj.get("mark_id") or 0)
    label = str(resp_obj.get("label") or kw)[:32]
    x_min = float(resp_obj.get("x_min", -1))
    y_min = float(resp_obj.get("y_min", -1))
    x_max = float(resp_obj.get("x_max", -1))
    y_max = float(resp_obj.get("y_max", -1))

    has_box = (0 <= x_min < x_max <= 1000) and (0 <= y_min < y_max <= 1000)
    vx = int(((x_min + x_max) / 2000.0) * sw) if has_box else -1
    vy = int(((y_min + y_max) / 2000.0) * sh) if has_box else -1
    vw = int(((x_max - x_min) / 1000.0) * sw) if has_box else 0
    vh = int(((y_max - y_min) / 1000.0) * sh) if has_box else 0

    final_x, final_y, src_tag = -1, -1, ""

    if 1 <= mark_id <= len(marked_subset):
        el = marked_subset[mark_id - 1]
        if not has_box or (abs(el["cx"] - vx) <= max(200, vw) and abs(el["cy"] - vy) <= max(150, vh)):
            final_x, final_y = el["cx"], el["cy"]
            src_tag = f"SoM#{mark_id}「{el['name'][:24] or label}」"

    if final_x < 0 and has_box:
        final_x, final_y = vx, vy
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
                fx1 = float(fine_obj.get("x_min", -1))
                fy1 = float(fine_obj.get("y_min", -1))
                fx2 = float(fine_obj.get("x_max", -1))
                fy2 = float(fine_obj.get("y_max", -1))
                if 0 <= fx1 < fx2 <= 1000 and 0 <= fy1 < fy2 <= 1000:
                    final_x = cx1 + int(((fx1 + fx2) / 2000.0) * crop_w)
                    final_y = cy1 + int(((fy1 + fy2) / 2000.0) * crop_h)
                    src_tag = f"高清精修定位「{label}」"

    if final_x < 0 or final_y < 0:
        return None

    final_x = max(2, min(sw - 2, final_x))
    final_y = max(2, min(sh - 2, final_y))

    try:
        dbg = som_img.copy()
        d = ImageDraw.Draw(dbg)
        d.ellipse([final_x - 16, final_y - 16, final_x + 16, final_y + 16], outline=(16, 185, 129), width=4)
        d.line([final_x - 26, final_y, final_x + 26, final_y], fill=(16, 185, 129), width=3)
        d.line([final_x, final_y - 26, final_x, final_y + 26], fill=(16, 185, 129), width=3)
        tmp_img = grounding_img.with_name(f".{grounding_img.name}.{os.getpid()}.tmp")
        dbg.resize((1280, int(sh * (1280 / sw)))).save(tmp_img, format="JPEG", quality=82)
        os.replace(tmp_img, grounding_img)
    except Exception:
        pass

    return final_x, final_y, src_tag


def look_at_screen(question: str = "请简明描述当前电脑屏幕上正在显示的主要内容。") -> str:
    ig = facade_attr("ImageGrab", ImageGrab)
    call_vlm = facade_attr("_call_vision_llm", _call_vision_llm)
    with _gui_lock:
        img = ig.grab().convert("RGB")
    w, h = img.size
    if w > 1600:
        img = img.resize((1600, int(h * (1600 / w))))

    prompt_q = f"用户正在通过语音询问他当前的电脑屏幕：{question}\n请用清晰简洁的中文直接给出准确观察结果或解答（控制在120字以内，便于语音播报）。"
    ans = call_vlm(prompt_q, img, json_mode=False)
    if not ans:
        return "屏幕视觉识别接口暂不可用，请检查 DEEPSEEK_API_KEY 或 GEMINI_API_KEY。"
    return f"屏幕画面分析结果：{ans}"
