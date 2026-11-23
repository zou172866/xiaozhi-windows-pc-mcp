"""Device archive selections and the short, confirmed SillyTavern handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing

import xz_common

_DEVICE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
_CHAT_KEY = re.compile(r"[a-f0-9]{64}")
_MAC_DASH = re.compile(r"[0-9a-f]{2}(?:-[0-9a-f]{2}){5}")


def validate_device_id(value: str) -> str:
    device = value.strip().lower() if isinstance(value, str) else ""
    if not _DEVICE.fullmatch(device):
        raise ValueError("请输入有效的设备 ID")
    return device.replace("-", ":") if _MAC_DASH.fullmatch(device) else device


def scoped_character_id(base_character_id, archive: str, device_id: str, chat_key: str = "") -> str:
    if archive == "daily":
        return base_character_id
    if archive != "tavern" or (chat_key and not _CHAT_KEY.fullmatch(chat_key)):
        raise ValueError("无效的酒馆存档")
    # A story follows the device and the exported chat, even if its daily persona changes.
    identity = validate_device_id(device_id) + "/" + chat_key
    return "starc_" + hashlib.sha256(identity.encode("ascii")).hexdigest()[:40]


def _path():
    return os.path.join(xz_common.STATE_DIR, "memory_archives.sqlite")


def _open_for_write():
    os.makedirs(xz_common.STATE_DIR, exist_ok=True)
    db = sqlite3.connect(_path())
    try:
        db.execute("CREATE TABLE IF NOT EXISTS devices (device TEXT PRIMARY KEY, "
                   "archive TEXT NOT NULL CHECK(archive IN ('daily','tavern')), chat_key TEXT NOT NULL DEFAULT '')")
        db.execute("CREATE TABLE IF NOT EXISTS handoffs (character_id TEXT PRIMARY KEY, "
                   "last_index INTEGER NOT NULL, snapshot TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS imported (character_id TEXT, idx INTEGER, digest TEXT NOT NULL, "
                   "PRIMARY KEY (character_id, idx))")
        db.execute("BEGIN IMMEDIATE")
        return db
    except BaseException:
        db.close()
        raise


def selected_binding(device_id):
    """Snapshot once per connection. Only a missing binding defaults to daily."""
    try:
        device = validate_device_id(device_id)
    except ValueError:
        return "daily", "", (), -1
    try:
        os.stat(_path())
    except FileNotFoundError:
        return "daily", "", (), -1
    with closing(sqlite3.connect(_path())) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT archive, chat_key FROM devices WHERE device=?", (device,)).fetchone()
        if not row or row[0] == "daily":
            return "daily", "", (), -1
        archive, chat_key = row
        cid = scoped_character_id(None, archive, device, chat_key)
        story = db.execute("SELECT snapshot, last_index FROM handoffs WHERE character_id=?", (cid,)).fetchone()
        turns = tuple((u, a) for u, a in json.loads(story[0])) if story else ()
        return archive, chat_key, turns, story[1] if turns else -1


def chosen_archive(device_id):
    return selected_binding(device_id)[0]


def select_archive(device_id, archive):
    device = validate_device_id(device_id)
    if archive not in ("daily", "tavern"):
        raise ValueError("存档只能选择 daily 或 tavern")
    with closing(_open_for_write()) as db, db:
        db.execute("INSERT INTO devices(device, archive) VALUES (?, ?) "
                   "ON CONFLICT(device) DO UPDATE SET archive=excluded.archive", (device, archive))
    return archive


def _parse_handoff(handoff):
    if not isinstance(handoff, dict) or set(handoff) != {"format", "chat_key", "turns"}:
        raise ValueError("酒馆交接文件字段不正确")
    key, turns = handoff["chat_key"], handoff["turns"]
    if handoff["format"] != "xiaozhi-tavern-v1" or not isinstance(key, str) or not _CHAT_KEY.fullmatch(key):
        raise ValueError("酒馆交接格式或聊天标识无效")
    if not isinstance(turns, list) or not 1 <= len(turns) <= 3:
        raise ValueError("交接文件应包含最近 1 至 3 轮对白")
    result = []
    previous = -1
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) != {"index", "user_message", "assistant_reply"}:
            raise ValueError("对白字段不正确")
        idx = turn["index"]
        if type(idx) is not int or not previous < idx <= 2**53 - 1:
            raise ValueError("对白序号无效或顺序错误")
        pair = (turn["user_message"], turn["assistant_reply"])
        for message in pair:
            if not isinstance(message, str) or not message.strip() or len(message) > 2000:
                raise ValueError("每段对白须为 1 至 2000 个字符")
            try:
                message.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("对白含有无效的 Unicode 字符") from exc
        result.append((idx, pair[0].strip(), pair[1].strip()))
        previous = idx
    return key, result


class HandoffConflict(ValueError):
    """An older or edited transcript cannot replace already confirmed history."""


def import_handoff(device_id, handoff, enqueue):
    device = validate_device_id(device_id)
    key, turns = _parse_handoff(handoff)
    cid = scoped_character_id(None, "tavern", device, key)
    with closing(_open_for_write()) as db, db:
        prior = db.execute("SELECT last_index FROM handoffs WHERE character_id=?", (cid,)).fetchone()
        if prior and turns[-1][0] < prior[0]:
            raise HandoffConflict("这份对白早于已导入的进度，请在酒馆导出最新对白或另开分支")
        pending = []
        for idx, user, reply in turns:
            pair_json = json.dumps([user, reply], ensure_ascii=False)
            digest = hashlib.sha256(pair_json.encode("utf-8")).hexdigest()
            old = db.execute("SELECT digest FROM imported WHERE character_id=? AND idx=?", (cid, idx)).fetchone()
            if old:
                if old[0] != digest:
                    raise HandoffConflict("已导入的对白后来被修改，请在酒馆另开聊天分支后导出")
                continue
            db.execute("INSERT INTO imported VALUES (?, ?, ?)", (cid, idx, digest))
            pending.append(dict(request_id=f"{cid}-{idx}-{digest[:16]}", source="sillytavern",
                                device_id=device, user_message=user, assistant_reply=reply))
        if pending:
            # Outbox commits before selection. Its stable IDs also cover a crash between the two commits.
            enqueue(pending, character_id=cid)
            snapshot = json.dumps([(u, a) for _, u, a in turns], ensure_ascii=False)
            db.execute("INSERT OR REPLACE INTO handoffs VALUES (?, ?, ?)", (cid, turns[-1][0], snapshot))
        db.execute("INSERT INTO devices VALUES (?, 'tavern', ?) ON CONFLICT(device) DO UPDATE SET "
                   "archive='tavern', chat_key=excluded.chat_key", (device, key))
    return len(pending)


def acknowledge_handoff(device_id, chat_key, turns, last_index):
    """An old connection can acknowledge its own unchanged snapshot only."""
    if not turns:
        return
    cid = scoped_character_id(None, "tavern", device_id, chat_key)
    snapshot = json.dumps(turns, ensure_ascii=False)
    with closing(_open_for_write()) as db, db:
        db.execute("UPDATE handoffs SET snapshot='[]' WHERE character_id=? AND last_index=? AND snapshot=?",
                   (cid, last_index, snapshot))
