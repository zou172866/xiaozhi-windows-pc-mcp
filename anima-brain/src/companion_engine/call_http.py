"""Local authenticated websocket bridge; SDK sessions are not coupled to this server."""

import asyncio
import base64
import json
import secrets
import struct
from contextlib import suppress

from starlette.websockets import WebSocketDisconnect

from .calls import CallOptions, CallSession
from .sdk_models import ImageInput


def call_endpoint(runtime, token, hosts):
    async def handle(ws):
        if ws.headers.get("host") not in hosts or ws.headers.get("origin") not in {
            "http://" + h for h in hosts
        }:
            await ws.close(code=1008)
            return
        await ws.accept()
        session = None
        sender = None
        try:
            raw = await asyncio.wait_for(ws.receive_text(), 5)
            if len(raw) > 16000:
                raise ValueError("Oversized handshake")
            first = json.loads(raw)
            if not secrets.compare_digest(str(first.get("token", "")), token):
                await ws.close(code=1008)
                return
            session = CallSession(
                runtime, first["character_id"], CallOptions.model_validate(first.get("options", {}))
            )

            async def send_events():
                async for event in session.events():
                    if event.audio:
                        await ws.send_bytes(struct.pack("<I", event.sequence) + event.audio)
                    else:
                        await ws.send_json(event.model_dump())
                    if event.type == "closed":
                        await ws.close()

            sender = asyncio.create_task(send_events())
            # Receive concurrently during setup: a hangup must cancel a slow avatar start.
            opening = asyncio.create_task(session.open())
            try:
                while True:
                    packet = await asyncio.wait_for(ws.receive(), 45)
                    if packet["type"] == "websocket.disconnect":
                        break
                    if packet.get("bytes") is not None:
                        if opening.done():
                            opening.result()
                            await session.send_audio(packet["bytes"])
                        continue
                    raw = packet.get("text", "")
                    if len(raw) > 720000:
                        raise ValueError("Oversized call message")
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "hangup":
                        break
                    if kind == "ping":
                        await ws.send_json({"type": "pong", "data": {}, "sequence": 0})
                    elif kind == "interrupt" and opening.done():
                        await session.interrupt()
                    elif kind == "mute":
                        await session.mute(bool(msg.get("enabled")))
                    elif kind == "frame" and opening.done():
                        session.set_frame(
                            ImageInput(data=base64.b64decode(msg["data"], validate=True))
                            if msg.get("data")
                            else None
                        )
            finally:
                session.cancel_requested = True
                # Shield the paid create until its result can be located and closed. ASR/RTC
                # have finite setup deadlines; never abandon an untracked paid avatar create.
                with suppress(Exception):
                    await opening
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            with suppress(Exception):
                await ws.send_json(
                    {
                        "type": "error",
                        "data": {
                            ("message"): (
                                "Could not start the call. Check voice, ASR and video credentials; "
                                "no automatic retry."
                            )
                        },
                        "sequence": 0,
                    }
                )
        finally:
            if session:
                await session.close()
            if sender:
                sender.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await sender
            with suppress(Exception):
                await ws.close()

    return handle
