"""Raw 16kHz 16-bit mono PCM input passthrough for PC microphone WebSocket clients (Patch #23).

Design contract:
- Zero import-time side effects: call `install_pc_pcm_input_patch()` explicitly from `boot.install_all()`.
- When a client explicitly negotiates `audio_params.format == "pcm"` in its `hello` handshake
  (stored by upstream `helloHandle.py` onto `conn.audio_format`), `ConnectionHandler._decode_opus_packet`
  bypasses `opuslib_next.Decoder` and validates + forwards the raw 16-bit PCM frame straight into
  `SileroVAD`, speculative VAD, `SherpaASR`/cloud ASR, SenseVoice vocal cues, and voiceprint guard.
- All standard ESP32 devices (`audio_format == "opus"`) keep the exact upstream Opus decode path.
"""

from __future__ import annotations

import functools
from typing import Any, Optional

import patch_registry

MAX_PCM_FRAME_BYTES = 32768


def is_pcm_audio_connection(conn: Any) -> bool:
    """Return True iff the WebSocket connection explicitly negotiated raw PCM audio."""
    fmt = str(getattr(conn, "audio_format", "opus") or "opus").strip().lower()
    return fmt == "pcm"


def validate_pcm_frame(packet: Any) -> Optional[bytes]:
    """Validate an incoming 16-bit mono PCM frame (`2..32768` even bytes) or return None."""
    if not isinstance(packet, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(packet)
    n = len(raw)
    if n < 2 or n > MAX_PCM_FRAME_BYTES or (n % 2) != 0:
        return None
    return raw


def install_pc_pcm_input_patch() -> None:
    """Install fail-soft raw PCM passthrough on `ConnectionHandler._decode_opus_packet`."""
    from core.connection import ConnectionHandler

    def _wrap_decode(_orig):
        @functools.wraps(_orig)
        def _patched_decode_opus_packet(self, opus_packet: bytes, *args, **kwargs):
            if is_pcm_audio_connection(self):
                return validate_pcm_frame(opus_packet)
            return _orig(self, opus_packet, *args, **kwargs)

        return _patched_decode_opus_packet

    patch_registry.install_patch(
        "pc_pcm_audio_input",
        ConnectionHandler,
        "_decode_opus_packet",
        _wrap_decode,
        category="experience",
        target="core.connection.ConnectionHandler._decode_opus_packet",
        required_params=("self", "opus_packet"),
        require_async=False,
        flag_obj=ConnectionHandler,
        flag_attr="_pc_pcm_input_patched",
    )
