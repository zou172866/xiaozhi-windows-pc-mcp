"""Exercise the real SDK call pipeline with a PCM16 mono 16kHz WAV input.

Uses your configured providers and may incur charges. No microphone is accessed.
Example:
    python examples/call_from_wav.py --storage output/brain.sqlite3 \
        --character rina --input question.wav --output output/reply.wav
"""

import argparse
import asyncio
import wave
from pathlib import Path

from companion_engine import BrainConfig, CallOptions, CompanionBrain, Settings


async def main(args):
    with wave.open(str(args.input), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("Input must be PCM16 mono 16kHz WAV")
        if source.getnframes() > 16000 * 30:
            raise ValueError("Use an input of at most 30 seconds")
        pcm = source.readframes(source.getnframes())
    settings = Settings.from_env()
    async with CompanionBrain(
        args.storage, settings=settings, config=BrainConfig(start_worker=False)
    ) as brain:
        async with brain.call(
            args.character, CallOptions(language=args.language, max_seconds=90)
        ) as call:
            complete = asyncio.Event()
            output = bytearray()

            async def collect():
                async for event in call.events():
                    if event.audio:
                        output.extend(event.audio)
                    elif event.type == "transcript":
                        print(event.data["speaker"] + ": " + event.data["text"], flush=True)
                    elif event.type in {"error", "notice"}:
                        print(event.data["message"], flush=True)
                    if event.type in {"audio_end", "error", "closed"}:
                        complete.set()

            consumer = asyncio.create_task(collect())
            try:
                for offset in range(0, len(pcm), 3200):
                    if complete.is_set():
                        break
                    await call.send_audio(pcm[offset : offset + 3200])
                    await asyncio.sleep(0.1)
                async with asyncio.timeout(60):
                    while not complete.is_set():
                        await call.send_audio(bytes(3200))
                        await asyncio.sleep(0.1)
            finally:
                await call.close()
                await consumer
            if not output:
                raise RuntimeError("The call returned no speech; inspect call_history()")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(args.output), "wb") as destination:
                destination.setnchannels(1)
                destination.setsampwidth(2)
                destination.setframerate(24000)
                destination.writeframes(output)
            print(f"Saved reply audio to {args.output}. Call receipt: {call.id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--character", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", choices=["zh-CN", "en-US", "ja-JP"], default="en-US")
    asyncio.run(main(parser.parse_args()))
