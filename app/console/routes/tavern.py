"""Confirm a local SillyTavern export and queue its selected dialogue."""

from http import HTTPStatus

import memory_archives
from console import shared


def handle_post_handoff(handler):
    body = handler._read_body_json()
    if set(body) != {"device_id", "handoff"}:
        raise shared._BadRequestError("交接需要设备 ID 和酒馆导出文件")

    def enqueue(turns, *, character_id):
        import anima_memory_provider

        return anima_memory_provider.get_default_outbox().enqueue(turns, character_id=character_id)

    try:
        count = memory_archives.import_handoff(body["device_id"], body["handoff"], enqueue)
    except memory_archives.HandoffConflict as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
    except ValueError as exc:
        raise shared._BadRequestError(str(exc)) from exc
    except Exception as exc:
        shared._log_rate_limited("tavern_handoff", type(exc).__name__)
        handler._send_json({"ok": False, "error": "酒馆交接未完成，请稍后用同一文件重试"}, HTTPStatus.SERVICE_UNAVAILABLE)
    else:
        handler._send_json({"ok": True, "archive": "tavern", "imported": count})
