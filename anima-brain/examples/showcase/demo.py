"""Explicit offline fixtures for the example and UI tests, never a model substitute.

The interface labels this mode throughout. It uses the real Engine storage/review paths,
but these simple scripted replies make no claims about conversational quality.
"""

import json
import re

from companion_engine.providers import Completion


class DemoProvider:
    model = "offline-scripted-preview"

    def complete(self, context):
        message = context.user_message
        payload = {
            "assistant_reply": (
                "I’m here. This is a scripted preview; connect a model to have a real conversation."
            )
        }
        match = re.search(r"remember that (.+)", message, re.IGNORECASE)
        if match:
            detail = match.group(1).strip()
            payload["memories"] = [
                {
                    "key": "preview-preference",
                    "text": detail,
                    "category": "preference",
                    "source_quote": detail,
                }
            ]
            payload["assistant_reply"] = (
                "A quiet evening it is. Music on, phone away, nowhere you need to be."
            )
        elif "portfolio review" in message.lower():
            payload["assistant_reply"] = (
                "You can be excited and nervous at the same time. "
                "Want to talk it through, or would you rather have a little company?"
            )
        elif "no pep talk" in message.lower():
            payload["assistant_reply"] = (
                "Okay, no pep talk. I’m here. What are you listening to tonight?"
            )
        elif "remember" in message.lower() and context.memories:
            payload["assistant_reply"] = "Here’s one thing you told me: " + context.memories[0].text
        return Completion(json.dumps(payload), self.model, None)


def seed(brain):
    if brain.characters():
        return
    brain.create_character(
        character_id="rina-preview",
        name="Rina",
        persona="Warm, curious, a little playful. A lover of quiet city walks, "
        "late-night playlists, and the details everyone else walks past. "
        "Listen before offering advice. Respect boundaries. A fictional companion.",
    )
    brain.create_character(
        character_id="sean-preview",
        name="Sean",
        persona="Thoughtful, quietly funny, and grounded. Photography, coffee, "
        "and slow mornings. Speak naturally. A fictional companion.",
    )
    for i, message in enumerate(
        [
            "Remember that I prefer quiet evenings to big parties.",
            "I have that portfolio review tomorrow. I’m excited, but also kind of nervous.",
            "Just company for now. No pep talk.",
        ]
    ):
        brain.chat_sync("rina-preview", message, request_id=f"preview-{i}")
