"""Real-model terminal chat using the Python SDK. No bundled chat frontend.

Run: python examples/chat.py --help
Provider credentials come from explicit COMPANION_* environment/settings configuration.
No demo-generated responses, persona service, Node server or hosted database is required.
"""

import argparse
from pathlib import Path
from uuid import uuid4

from companion_engine import BrainConfig, ChatFailed, CompanionBrain, Conflict, Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./output/quickstart/brain.sqlite3")
    parser.add_argument("--user", default="local")
    parser.add_argument("--character", default="mika")
    parser.add_argument("--name", default="Mika")
    parser.add_argument("--persona", default="Direct, playful, caring without empty reassurance.")
    parser.add_argument(
        "--memory-policy", choices=["automatic", "review", "off"], default="automatic"
    )
    parser.add_argument("--no-relationship", action="store_true")
    parser.add_argument("--recent-turns", type=int, default=6)
    parser.add_argument("--context-chars", type=int, default=48000)
    parser.add_argument("--reader-steps", type=int, choices=[0, 1, 2], default=0)
    parser.add_argument("--reply-instructions-file", type=Path)
    parser.add_argument("--capture-context", action="store_true")
    parser.add_argument("--message", help="Generate one reply and exit, useful for smoke tests")
    args = parser.parse_args()
    settings = Settings.from_env()  # Deliberate opt-in, unlike the library constructor.
    settings.reader_max_steps = args.reader_steps
    options = {
        "recent_turns": args.recent_turns,
        "context_chars": args.context_chars,
        "capture_context": args.capture_context,
    }
    if args.reply_instructions_file:
        options["reply_instructions"] = args.reply_instructions_file.read_text(encoding="utf-8")
    with CompanionBrain(
        args.db,
        user_id=args.user,
        settings=settings,
        config=BrainConfig(**options),
    ) as brain:
        if not any(c.id == args.character for c in brain.characters()):
            brain.create_character(
                character_id=args.character,
                name=args.name,
                persona=args.persona,
                memory_policy=args.memory_policy,
                relationship_enabled=not args.no_relationship,
            )
        # Existing characters retain their saved identity/policy; no implicit replacement.
        if not settings.model or not settings.base_url:
            parser.error("Set COMPANION_BASE_URL and COMPANION_MODEL; see config.example.env.")
        print(
            "Enter /quit to exit; /memories to inspect facts; /pending to inspect interrupted work."
        )
        print(
            "Existing characters keep their saved settings. Memory 'off' still stores chat history."
        )
        while True:
            try:
                message = args.message if args.message is not None else input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if message == "/quit":
                break
            if message == "/memories":
                print([m.model_dump() for m in brain.memories(args.character)])
            elif message == "/pending":
                print(brain.pending(args.character))
            elif message:
                request_id = str(uuid4())
                print("Request:", request_id)
                try:
                    reply = brain.chat_sync(args.character, message, request_id=request_id)
                    print(f"{args.name}: {reply.text}")
                    if reply.needs_review:
                        print("Proposals:", [m.text for m in reply.proposed_memories])
                        print("Corrections:", [c.draft.text for c in reply.proposed_corrections])
                        retain = input("Save these memory changes? [y/N] ").lower() == "y"
                        reply = brain.approve(args.character, reply.turn_id, retain=retain)
                    print(f"Saved: {reply.committed}; model calls: {reply.usage.attempts}")
                except (ChatFailed, Conflict) as exc:
                    print(str(exc))
                    if isinstance(exc, ChatFailed):
                        print("Pending turn:", exc.pending_turn_id, "request:", exc.request_id)
                    print("Inspect /pending and receipts before retrying; nothing auto-retried.")
            if args.message is not None:
                break


if __name__ == "__main__":
    main()
