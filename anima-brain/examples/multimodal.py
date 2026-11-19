"""Explicit BYOK media requests. No automatic image, voice or embedding calls.

python examples/multimodal.py --help
python examples/multimodal.py vision --image outfit.jpg --prompt 'How does this look?'
python examples/multimodal.py image --prompt 'Reading in a rainy cafe' --size 1024x1536
python examples/multimodal.py speak --prompt 'Hey, how did it go?' --voice-id your-voice
python examples/multimodal.py transcribe --audio message.webm --mime audio/webm
"""

import argparse
from pathlib import Path
from uuid import uuid4

from companion_engine import (
    AudioInput,
    BrainConfig,
    CompanionBrain,
    ImageInput,
    ImageOptions,
    Settings,
    SpeechOptions,
    TranscriptionOptions,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["vision", "image", "speak", "transcribe"])
    parser.add_argument("--db", default="./output/multimodal/brain.sqlite3")
    parser.add_argument("--user", default="local")
    parser.add_argument("--character", default="mika")
    parser.add_argument("--request-id", help="Reuse this ID only for an exact completed request")
    parser.add_argument("--prompt")
    parser.add_argument("--image", action="append", type=Path, default=[])
    parser.add_argument("--reference", action="append", type=Path, default=[])
    parser.add_argument("--detail", choices=["auto", "low", "high"], default="auto")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", choices=["auto", "low", "medium", "high"], default="medium")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--voice-id")
    parser.add_argument("--appearance", default="Short dark hair, round glasses, a blue sweater.")
    parser.add_argument("--audio", type=Path)
    parser.add_argument(
        "--mime", choices=["audio/webm", "audio/mp4", "audio/wav", "audio/mpeg", "audio/ogg"]
    )
    parser.add_argument("--language", help="Optional two-letter transcription language")
    args = parser.parse_args()
    if args.mode != "transcribe" and not args.prompt:
        parser.error("--prompt is required for vision, image and speak")
    if args.mode == "vision" and not args.image:
        parser.error("vision requires at least one --image")
    if args.mode == "transcribe" and not (args.audio and args.mime):
        parser.error("transcribe requires --audio and --mime")
    request_id = args.request_id or str(uuid4())
    print("Request:", request_id)
    settings = Settings.from_env()
    settings.reader_max_steps = 0
    with CompanionBrain(
        args.db, user_id=args.user, settings=settings, config=BrainConfig(start_worker=False)
    ) as brain:
        if not any(c.id == args.character for c in brain.characters()):
            brain.create_character(
                character_id=args.character,
                name="Mika",
                persona="Warm, direct, concise.",
                appearance=args.appearance,
                voice_id=args.voice_id or "",
                relationship_enabled=False,
            )
        # Existing identity, appearance, memory and automation policies are preserved.
        if args.mode == "vision":
            result = brain.chat_sync(
                args.character,
                args.prompt,
                request_id=request_id,
                attachments=[ImageInput(path=p, detail=args.detail) for p in args.image],
            )
        elif args.mode == "image":
            result = brain.generate_image_sync(
                args.character,
                args.prompt,
                request_id=request_id,
                options=ImageOptions(size=args.size, quality=args.quality, count=args.count),
                references=[ImageInput(path=p) for p in args.reference],
            )
        elif args.mode == "speak":
            result = brain.speak_sync(
                args.character,
                args.prompt,
                request_id=request_id,
                options=SpeechOptions(voice_id=args.voice_id),
            )
        else:
            result = brain.transcribe_sync(
                args.character,
                AudioInput(path=args.audio, mime=args.mime),
                request_id=request_id,
                options=TranscriptionOptions(language=args.language),
            )
        print(result.model_dump_json(indent=2))
        if getattr(result, "needs_review", False):
            print("Review this draft, then call brain.approve(...) with the displayed turn_id.")


if __name__ == "__main__":
    main()
