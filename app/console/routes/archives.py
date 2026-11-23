"""Per-device memory archive selection for the next voice connection."""

from http import HTTPStatus
import sqlite3
from urllib.parse import parse_qs

import memory_archives


def handle_get_memory_archive(handler, parsed) -> None:
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=1)
        if set(query) != {"device_id"}:
            raise ValueError("请选择设备")
        device = memory_archives.validate_device_id(query["device_id"][0])
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    try:
        archive = memory_archives.chosen_archive(device)
    except (ValueError, OSError, sqlite3.Error):
        handler._send_json({"ok": False, "error": "无法读取记忆存档设置"}, HTTPStatus.SERVICE_UNAVAILABLE)
        return
    handler._send_json({"ok": True, "device_id": device, "archive": archive})


def handle_post_memory_archive(handler) -> None:
    body = handler._read_body_json()
    try:
        if set(body) != {"device_id", "archive"}:
            raise ValueError("请提交设备标识和存档名称")
        device = memory_archives.validate_device_id(body["device_id"])
        archive = body["archive"]
        if archive not in ("daily", "tavern"):
            raise ValueError("请选择日常陪伴或酒馆剧情存档")
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    try:
        selected = memory_archives.select_archive(device, archive)
    except (ValueError, OSError, sqlite3.Error):
        handler._send_json({"ok": False, "error": "无法保存记忆存档设置"}, HTTPStatus.SERVICE_UNAVAILABLE)
        return
    handler._send_json({"ok": True, "device_id": device, "archive": selected})
