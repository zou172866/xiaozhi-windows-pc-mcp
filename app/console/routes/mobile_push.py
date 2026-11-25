"""18080 Web Console routes for Phase 3 domestic lightweight mobile push bridge (⑥)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Dict

import mobile_push
import runtime_settings
from console import shared
from console.routes import anima as anima_routes

_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "provider",
        "forward_proactive",
        "forward_briefing",
        "respect_quiet_hours",
        "max_daily_pushes",
        "secrets",
    }
)
_ALLOWED_TEST_KEYS = frozenset({"provider", "message"})
_ALLOWED_BRIEFING_KEYS = frozenset({"target_date", "provider"})


def _validate_mobile_push_config(payload: dict) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("请提供要更新的手机推送配置字段")
    unknown = sorted(str(k) for k in payload.keys() if k not in _ALLOWED_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"包含未允许的字段: {', '.join(unknown[:6])}")

    cfg_updates: Dict[str, Any] = {}
    for bkey in ("enabled", "forward_proactive", "forward_briefing", "respect_quiet_hours"):
        if bkey in payload:
            val = payload[bkey]
            if not isinstance(val, bool):
                raise ValueError(f"字段 {bkey} 必须为布尔值")
            cfg_updates[bkey] = val

    if "provider" in payload:
        raw_p = payload["provider"]
        if not isinstance(raw_p, str):
            raise ValueError("字段 provider 必须为字符串")
        p = raw_p.strip().lower()
        if p not in runtime_settings.VALID_MOBILE_PUSH_PROVIDERS:
            raise ValueError("provider 仅支持 bark、feishu、wecom、pushplus 或 serverchan")
        cfg_updates["provider"] = p

    if "max_daily_pushes" in payload:
        raw_m = payload["max_daily_pushes"]
        if isinstance(raw_m, bool) or not isinstance(raw_m, int):
            raise ValueError("字段 max_daily_pushes 必须为 1～50 之间的整数")
        if raw_m < 1 or raw_m > 50:
            raise ValueError("字段 max_daily_pushes 必须在 1～50 之间")
        cfg_updates["max_daily_pushes"] = raw_m

    sec_updates: Dict[str, Any] = {}
    if "secrets" in payload:
        raw_s = payload["secrets"]
        if not isinstance(raw_s, dict):
            raise ValueError("字段 secrets 必须为 JSON 对象")
        for sk, sv in raw_s.items():
            if sk not in mobile_push.SECRET_FIELD_ENV_MAP:
                raise ValueError(f"包含未允许的凭据字段: {sk}")
            if sv is not None and not isinstance(sv, str):
                raise ValueError(f"凭据字段 {sk} 必须为字符串或 null")
            sec_updates[sk] = sv

    return cfg_updates, sec_updates


def handle_get_mobile_push(handler) -> None:
    try:
        handler._send_json(mobile_push.get_mobile_push_bridge().get_status())
    except Exception as exc:
        shared._log_rate_limited("mp_get_status", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_mobile_push_config(handler) -> None:
    try:
        body = handler._read_body_json()
        cfg_updates, sec_updates = _validate_mobile_push_config(body)
        bridge = mobile_push.get_mobile_push_bridge()
        if sec_updates:
            bridge.update_secrets(sec_updates)
        if cfg_updates:
            runtime_settings.set_mobile_push(**cfg_updates)
        handler._send_json(bridge.get_status())
    except shared._BadRequestError:
        raise
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        shared._log_rate_limited("mp_post_config", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_mobile_push_test(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_TEST_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        prov = body.get("provider")
        if prov is not None:
            if not isinstance(prov, str) or prov.strip().lower() not in runtime_settings.VALID_MOBILE_PUSH_PROVIDERS:
                handler._send_json({"ok": False, "error": "字段 provider 非法"}, HTTPStatus.BAD_REQUEST)
                return
            prov = prov.strip().lower()
        msg = body.get("message")
        if msg is not None and not isinstance(msg, str):
            handler._send_json({"ok": False, "error": "字段 message 必须为字符串"}, HTTPStatus.BAD_REQUEST)
            return
        test_body = (msg or "").strip()[:200] or "这是一条来自小智 18080 控制台的手机推送连通性测试消息。"
        bridge = mobile_push.get_mobile_push_bridge()
        res = bridge.send(
            "小智 · 手机通知测试",
            test_body,
            category="test",
            provider_override=prov,
            bypass_limits=True,
        )
        status_payload = bridge.get_status()
        code = HTTPStatus.OK if res.get("ok") else (
            HTTPStatus.BAD_REQUEST if res.get("reason") == "invalid_config" else HTTPStatus.BAD_GATEWAY
        )
        handler._send_json(
            {
                "ok": bool(res.get("ok")),
                "result": res,
                "error": str(res.get("error") or ""),
                "mobile_push": status_payload,
            },
            status=code,
        )
    except shared._BadRequestError:
        raise
    except Exception as exc:
        shared._log_rate_limited("mp_post_test", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def handle_post_mobile_push_briefing(handler) -> None:
    try:
        body = handler._read_body_json()
        unknown = sorted(str(k) for k in body.keys() if k not in _ALLOWED_BRIEFING_KEYS)
        if unknown:
            handler._send_json(
                {"ok": False, "error": f"包含未允许的字段: {', '.join(unknown[:6])}"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        prov = body.get("provider")
        if prov is not None:
            if not isinstance(prov, str) or prov.strip().lower() not in runtime_settings.VALID_MOBILE_PUSH_PROVIDERS:
                handler._send_json({"ok": False, "error": "字段 provider 非法"}, HTTPStatus.BAD_REQUEST)
                return
            prov = prov.strip().lower()

        bridge = mobile_push.get_mobile_push_bridge()
        if not bridge.is_enabled():
            handler._send_json(
                {
                    "ok": False,
                    "error": "手机端轻量通知推送当前处于关闭状态，请先打开总开关并配置通道凭据。",
                    "mobile_push": bridge.get_status(),
                },
                HTTPStatus.FORBIDDEN,
            )
            return

        brief_payload = {}
        if "target_date" in body and body["target_date"] is not None:
            brief_payload["target_date"] = body["target_date"]
        ok_br, br_res = anima_routes._trigger_obsidian_briefing(brief_payload)
        if not ok_br or not isinstance(br_res, dict):
            code = HTTPStatus.BAD_REQUEST if "target_date" in str(br_res) else HTTPStatus.BAD_GATEWAY
            handler._send_json(
                {"ok": False, "error": str(br_res), "mobile_push": bridge.get_status()},
                status=code,
            )
            return

        r_data = br_res.get("result") or {}
        obs_data = br_res.get("obsidian") or {}
        summary = str(
            r_data.get("briefing_summary")
            or ((obs_data.get("briefing") or {}).get("summary") or "")
        ).strip()
        day_label = str(r_data.get("daily_date") or ((obs_data.get("briefing") or {}).get("daily_date") or "今日"))
        if not summary:
            st = r_data.get("stats") or {}
            summary = (
                f"{day_label} 离线简报已生成：今日对话 {st.get('daily_turns', 0)} 轮，"
                f"待跟进事项 {st.get('open_loops', 0)} 项。"
            )

        push_res = bridge.send(
            f"小智每日简报（{day_label}）",
            summary,
            category="briefing",
            provider_override=prov,
            bypass_limits=True,
        )
        code = HTTPStatus.OK if push_res.get("ok") else (
            HTTPStatus.BAD_REQUEST if push_res.get("reason") == "invalid_config" else HTTPStatus.BAD_GATEWAY
        )
        handler._send_json(
            {
                "ok": bool(push_res.get("ok")),
                "result": push_res,
                "briefing": r_data,
                "obsidian": obs_data,
                "error": str(push_res.get("error") or ""),
                "mobile_push": bridge.get_status(),
            },
            status=code,
        )
    except shared._BadRequestError:
        raise
    except Exception as exc:
        shared._log_rate_limited("mp_post_briefing", exc)
        handler._send_json(
            {"ok": False, "error": shared._redact_secrets(str(exc))},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
