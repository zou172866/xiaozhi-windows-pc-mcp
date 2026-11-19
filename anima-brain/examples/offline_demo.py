"""A scripted integration demo, NOT a generated conversation. No API calls."""

from pathlib import Path
from tempfile import TemporaryDirectory

from companion_engine import CharacterSpec, CommitRequest, Engine, MemoryDraft


def main():
    with TemporaryDirectory(prefix="companion-demo-") as directory:
        database = Path(directory) / "brain.sqlite3"
        with Engine(database, owner="demo") as brain:
            brain.create_character(
                CharacterSpec(
                    id="nova", name="Nova", persona="A curious, understated fictional companion."
                )
            )
            context = brain.prepare_turn("nova", "记住，我喜欢乌龙茶。", request_id="first")
            brain.commit_turn(
                "nova",
                context.turn_id,
                CommitRequest(
                    assistant_reply="好，下次聊到喝什么，我会记得。",
                    memories=[
                        MemoryDraft(
                            key="preferred_drink",
                            text="喜欢乌龙茶",
                            source_quote="我喜欢乌龙茶",
                            category="preference",
                        )
                    ],
                ),
            )
            print("1. Stored a user-approved memory with its exact source quote.")
        with Engine(database, owner="demo") as brain:
            memory = brain.search("nova", "乌龙茶")[0]
            print(f"2. Reopened database; retrieved: {memory.text}")
            corrected = brain.correct_memory(
                "nova",
                memory.id,
                MemoryDraft(
                    key="preferred_drink",
                    text="现在更喜欢咖啡",
                    source_quote="现在更喜欢咖啡",
                    category="preference",
                ),
                expected_revision=memory.revision,
            )
            context = brain.prepare_turn("nova", "喝点什么？", request_id="second")
            assert any(item.id == corrected.id and "咖啡" in item.text for item in context.memories)
            print(f"3. Corrected to revision {corrected.revision}: {corrected.text}")
            print("4. Prepared next turn with the corrected fact, persona and response guidance.")
            print(
                "5. Old fact is absent from active memory search:", brain.search("nova", "乌龙茶")
            )
            brain.abort_turn("nova", context.turn_id)
        with Engine(database, owner="another-user") as other:
            assert other.characters() == []
            print("6. Another owner cannot see this character or its memories.")
    print("Demo complete. Temporary synthetic data removed; no production data or paid API used.")


if __name__ == "__main__":
    main()
