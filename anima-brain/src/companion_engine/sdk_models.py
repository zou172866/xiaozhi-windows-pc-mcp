"""Small typed SDK inputs. Local file bytes never enter a JSON log or model repr."""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import Model


class MemoryConfig(Model):
    """Read policy, independent of CharacterSpec.memory_policy (writes)."""

    read_enabled: bool = True
    evidence_limit: int = Field(default=8, ge=0, le=30, strict=True)
    fact_limit: int = Field(default=12, ge=0, le=30, strict=True)
    document_limit: int = Field(default=8, ge=0, le=30, strict=True)
    context_chars: int = Field(default=16000, ge=0, le=100000, strict=True)


def image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise ValueError("Expected PNG, JPEG or WebP image bytes")


@dataclass(frozen=True)
class LoadedImage:
    data: bytes = field(repr=False)
    mime: str
    extension: str
    source_ref: str
    detail: str

    def descriptor(self):
        return {"source_ref": self.source_ref, "mime": self.mime, "detail": self.detail}


class ImageInput(Model):
    """Exactly one local path or byte buffer; no implicit remote URL downloads."""

    path: Path | None = Field(default=None, exclude=True, repr=False)
    data: bytes | None = Field(default=None, exclude=True, repr=False, strict=True)
    detail: Literal["auto", "low", "high"] = "auto"

    @model_validator(mode="after")
    def one_source(self):
        if (self.path is None) == (self.data is None):
            raise ValueError("Supply exactly one of path or data")
        return self

    def load(self) -> LoadedImage:
        limit = 10 * 1024 * 1024
        if self.path is not None:
            path = self.path.expanduser()
            if not path.is_file():
                raise ValueError("Image path must be a regular file")
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
        else:
            raw = self.data
        if not raw or len(raw) > limit:
            raise ValueError("Each image must contain 1 byte to 10MB")
        mime, extension = image_type(raw)
        return LoadedImage(
            raw, mime, extension, "sha256:" + hashlib.sha256(raw).hexdigest(), self.detail
        )


def load_images(images) -> list[LoadedImage]:
    if len(images) > 4:
        raise ValueError("At most four images per request")
    loaded = [ImageInput.model_validate(image).load() for image in images]
    if len({image.source_ref for image in loaded}) != len(loaded):
        raise ValueError("Duplicate image inputs")
    if sum(len(image.data) for image in loaded) > 20 * 1024 * 1024:
        raise ValueError("Combined image inputs exceed 20MB")
    return loaded


class ImageOptions(Model):
    """GPT Image-compatible parameters; model-specific support is checked by the provider."""

    size: str = Field(default="1024x1024", pattern=r"^(auto|[1-9][0-9]{1,4}x[1-9][0-9]{1,4})$")
    quality: Literal["auto", "low", "medium", "high", "standard", "hd"] = "medium"
    count: int = Field(default=1, ge=1, le=4, strict=True)
    output_format: Literal["png", "jpeg", "webp"] | None = None
    background: Literal["auto", "transparent", "opaque"] | None = None
    input_fidelity: Literal["low", "high"] | None = None

    @model_validator(mode="after")
    def compatible_format(self):
        if self.background == "transparent" and self.output_format == "jpeg":
            raise ValueError("JPEG does not support transparency")
        return self


class SpeechOptions(Model):
    voice_id: str | None = Field(default=None, min_length=1, max_length=200)
    # MiniMax exposes these controls. Other adapters reject explicit overrides.
    speed: float | None = Field(default=None, ge=0.5, le=2)
    volume: float | None = Field(default=None, ge=0.1, le=10)
    pitch: int | None = Field(default=None, ge=-12, le=12, strict=True)


class TranscriptionOptions(Model):
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    prompt: str | None = Field(default=None, max_length=2000)


class AudioInput(Model):
    path: Path | None = Field(default=None, exclude=True, repr=False)
    data: bytes | None = Field(default=None, exclude=True, repr=False, strict=True)
    mime: Literal["audio/webm", "audio/mp4", "audio/wav", "audio/mpeg", "audio/ogg"]

    @model_validator(mode="after")
    def one_source(self):
        if (self.path is None) == (self.data is None):
            raise ValueError("Supply exactly one of path or data")
        return self

    def load(self) -> bytes:
        limit = 20 * 1024 * 1024
        if self.path is not None:
            path = self.path.expanduser()
            if not path.is_file():
                raise ValueError("Audio path must be a regular file")
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
        else:
            raw = self.data
        if not raw or len(raw) > limit:
            raise ValueError("Audio must contain 1 byte to 20MB")
        return raw


class MediaArtifact(Model):
    artifact: str
    path: str
    mime: str
    bytes: int


class ImageResult(Model):
    character_id: str
    request_id: str
    artifacts: list[MediaArtifact]
    usage: dict = Field(default_factory=dict)


class SpeechResult(Model):
    character_id: str
    request_id: str
    artifact: MediaArtifact
    usage: dict = Field(default_factory=dict)


class TranscriptionResult(Model):
    character_id: str
    request_id: str
    text: str
    source_ref: str | None = None
    usage: dict = Field(default_factory=dict)
    sent_as_message: bool = False
