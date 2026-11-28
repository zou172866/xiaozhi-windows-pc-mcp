"""18080 Web Console routes for Phase 1 proactive companion, desktop rhythm & inner life."""

from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any, Dict

import proactive_companion
import runtime_settings
import vocal_cue_router
from console import shared

_ALLOWED_PROACTIVE_KEYS = frozenset(
    {
        "enabled",
        "delivery_mode",
        "check_interval_minutes",
        "quiet_start",
        "quiet_end",
        "min_since_last_chat_minutes",
        "min_cooldown_minutes",
        "max_daily_nudges",
        "pc_speaker_volume",
        "allow_idle_chitchat",
        "allow_l3_finish_notify",
        "allow_open_loop_followup",
        "inner_life_enabled",
        "allow_taskbar_inbox",
        "vocal_cue_enabled",
    }
)
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _validate_proactive_update(payload: dict) -> Dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("请提供要更新的主动陪伴配置字段")

    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_PROACTIVE_KEYS)
    if unknown:
        raise ValueError(f"包含未允许的字段: {', '.join(unknown[:6])}")

    cleaned: Dict[str, Any] = {}
    for bkey in (
        "enabled",
        "allow_idle_chitchat",
        "allow_l3_finish_notify",
        "allow_open_loop_followup",
        "inner_life_enabled",
        "allow_taskbar_inbox",
        "vocal_cue_enabled",
    ):
        if bkey in payload:
            val = payload[bkey]
            if not isinstance(val, bool):
                raise ValueError(f"字段 {bkey} 必须为布尔值")
            cleaned[bkey] = val

    if "delivery_mode" in payload:
        raw_m = payload["delivery_mode"]
        if not isinstance(raw_m, str):
            raise ValueError("字段 delivery_mode 必须为字符串 (dry_run / pc_speaker / mobile_push / pc_and_mobile)")
        m = raw_m.strip().lower()
        if m not in runtime_settings.VALID_PROACTIVE_DELIVERY_MODES:
            raise ValueError(
                "delivery_mode 仅支持 dry_run、pc_speaker、mobile_push 或 pc_and_mobile"
            )
        cleaned["delivery_mode"] = m

    for tkey in ("quiet_start", "quiet_end"):
        if tkey in payload:
            raw_t = payload[tkey]
            if not isinstance(raw_t, str) or not _HHMM_RE.match(raw_t.strip()):
                raise ValueError(f"字段 {tkey} 必须为 HH:MM 格式（如 23:00）")
            cleaned[tkey] = raw_t.strip()

    int_bounds = {
        "check_interval_minutes": (5, 120),
        "min_since_last_chat_minutes": (5, 720),
        "min_cooldown_minutes": (15, 720),
        "max_daily_nudges": (1, 12),
    }
    for ikey, (lo, hi) in int_bounds.items():
        if ikey in payload:
            raw_i = payload[ikey]
            if isinstance(raw_i, bool) or not isinstance(raw_i, int):
                raise ValueError(f"字段 {ikey} 必须为整数（{lo}～{hi}）")
            if raw_i < lo or raw_i > hi:
                raise ValueError(f"字段 {ikey} 超出允许范围（{lo}～{hi}）")
            cleaned[ikey] = raw_i

    if "pc_speaker_volume" in payload:
        raw_v = payload["pc_speaker_volume"]
        if isinstance(raw_v, bool):
            raise ValueError("字段 pc_speaker_volume 必须为 0.2～1.2 之间的数字")
        try:
            fv = float(raw_v)
        except (TypeError, ValueError) as exc:
            raise ValueError("字段 pc_speaker_volume 必须为 0.2～1.2 之间的数字") from exc
        if fv != fv or fv < 0.2 or fv > 1.2:
            raise ValueError("字段 pc_speaker_volume 必须在 0.2～1.2 之间")
        cleaned["pc_speaker_volume"] = round(fv, 2)

    return cleaned


def handle_get_proactive(handler) -> None:
    try:
        snap = proactive_companion.get_proactive_snapshot(refresh_live=True, history_limit=20)
        handler._send_json(snap)
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_proactive(handler) -> None:
    try:
        body = handler._read_body_json()
        cleaned = _validate_proactive_update(body)
        vocal_cue_en = cleaned.pop("vocal_cue_enabled", None)
        if vocal_cue_en is not None:
            runtime_settings.set_vocal_cue(enabled=vocal_cue_en)
            if not vocal_cue_en:
                vocal_cue_router.reset_vocal_cues()
        saved = runtime_settings.set_proactive(**cleaned) if cleaned else runtime_settings.get_proactive()
        try:
            running = proactive_companion.sync_proactive_lifecycle()
            eng = proactive_companion.get_proactive_engine()
            if "enabled" in cleaned or "allow_taskbar_inbox" in cleaned:
                want_inbox = bool(saved.get("enabled", False) and saved.get("allow_taskbar_inbox", False))
                eng.sync_host_inbox_state(want_inbox)
            if running:
                eng.wake()
        except Exception as exc:
            shared._log_rate_limited("proactive_wake", exc)
        snap = proactive_companion.get_proactive_snapshot(refresh_live=False, history_limit=20)
        snap["config"] = saved
        handler._send_json(snap)
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_proactive_run(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in ("force_dry_run", "bypass_timing_gates"))
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        force_dry_run = body.get("force_dry_run", True)
        bypass_timing = body.get("bypass_timing_gates", True)
        if not isinstance(force_dry_run, bool) or not isinstance(bypass_timing, bool):
            handler._send_json(
                {"ok": False, "error": "字段 force_dry_run 与 bypass_timing_gates 必须为布尔值"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        res = proactive_companion.get_proactive_engine().trigger_manual_check(
            force_dry_run=force_dry_run,
            bypass_timing_gates=bypass_timing,
        )
        snap = proactive_companion.get_proactive_snapshot(refresh_live=False, history_limit=20)
        code = HTTPStatus.OK if res.get("ok", True) else HTTPStatus.BAD_GATEWAY
        handler._send_json({"result": res, "proactive": snap, **res}, status=code)
    except shared._BadRequestError:
        raise
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_proactive_inner_life(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in ("use_llm",))
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        use_llm = body.get("use_llm", False)
        if not isinstance(use_llm, bool):
            handler._send_json(
                {"ok": False, "error": "字段 use_llm 必须为布尔值"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        res = proactive_companion.get_proactive_engine().refresh_inner_life_now(use_llm=use_llm)
        code = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY
        handler._send_json(res, status=code)
    except shared._BadRequestError:
        raise
    except Exception as exc:
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
