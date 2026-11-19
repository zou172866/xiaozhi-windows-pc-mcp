"""Packaged workflow and receipt-scoped MCP Apps audio delivery."""

import base64
import os
import stat
from pathlib import Path

from mcp.types import CallToolResult, TextContent

PLAYER_URI = "ui://companion-engine/voice-player-v3.html"
ROOT = Path(__file__).parent
SKILL_ROOT = ROOT / "plugin" / "skills" / "companion-conversation"
CHUNK_BYTES = 256 * 1024
MAX_BYTES = 25 * 1024 * 1024


def workflow(section="workflow"):
    path = {"workflow": SKILL_ROOT / "SKILL.md", "setup": SKILL_ROOT / "references/setup.md"}
    if section not in path:
        raise ValueError("Unknown workflow section")
    return path[section].read_text(encoding="utf-8")


def player_html():
    return (ROOT / "static" / "voice-player.html").read_text(encoding="utf-8")


def _open_audio(media, character_id, artifact):
    path, mime = media.artifact(character_id, artifact)
    if mime != "audio/mpeg":
        raise ValueError("This artifact is not a voice message")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError("Voice file is unavailable locally") from None
    stream = os.fdopen(fd, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
        stream.close()
        raise ValueError("Invalid voice file")
    return stream, info.st_size


def voice_card(media, character_id, artifact):
    with _open_audio(media, character_id, artifact)[0] as stream:
        size = os.fstat(stream.fileno()).st_size
    spec = next(c for c in media.runtime.engine.characters() if c.id == character_id)
    data = {
        "character_id": character_id,
        "character_name": spec.name,
        "artifact": artifact,
        "mime": "audio/mpeg",
        "bytes": size,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=f"Voice message from {spec.name}. Use the player.")],
        structured_content=data,
    )


def voice_chunk(media, character_id, artifact, offset):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Invalid audio offset")
    stream, total = _open_audio(media, character_id, artifact)
    with stream:
        if offset >= total:
            raise ValueError("Audio offset is beyond the file")
        stream.seek(offset)
        data = stream.read(CHUNK_BYTES)
    end = offset + len(data)
    return CallToolResult(
        content=[TextContent(type="text", text="Private audio chunk for the playback component.")],
        structured_content={
            "offset": offset,
            "next_offset": end,
            "total": total,
            "eof": end == total,
        },
        meta={"audio_base64": base64.b64encode(data).decode("ascii")},
    )
