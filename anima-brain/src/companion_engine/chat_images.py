"""Owner-scoped appearance uploads and bounded, journaled chat image actions."""

import json

from .engine import Conflict
from .models import CommitRequest
from .sdk_models import ImageInput


class ChatImages:
    def __init__(self, runtime):
        self.runtime, self.engine = runtime, runtime.engine
        with self.engine._transaction():
            self.engine.db.execute(
                "CREATE TABLE IF NOT EXISTS appearance_references (owner TEXT NOT NULL, "
                "character_id TEXT NOT NULL, metadata TEXT NOT NULL, "
                "PRIMARY KEY(owner,character_id), FOREIGN KEY(owner,character_id) "
                "REFERENCES characters(owner,id) ON DELETE CASCADE)"
            )

    def reference(self, character_id):
        with self.engine._lock:
            self.engine._character(character_id)
            row = self.engine.db.execute(
                "SELECT metadata FROM appearance_references WHERE owner=? AND character_id=?",
                (self.engine.owner, character_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def path(self, reference):
        name = reference["artifact"]
        if not name or "/" in name or "\\" in name:
            raise ValueError("Invalid reference file")
        path = self.runtime.media_dir / name
        if path.is_symlink() or path.parent.resolve() != self.runtime.media_dir.resolve():
            raise ValueError("Invalid reference file")
        return path

    def set_reference(self, character_id, image, *, save_character=None):
        from .media import Media

        saved = None
        try:
            with self.engine._transaction():
                if save_character:
                    save_character()
                previous = self.reference(character_id)
                if self.engine.pending_turn(character_id):
                    raise Conflict("Finish or discard the pending turn before changing appearance")
                if image:
                    saved = Media(self.runtime)._save(image.data, image.mime, image.extension, {})
                    value = {k: saved[k] for k in ("artifact", "mime", "bytes")}
                    value["source_ref"] = image.source_ref
                    self.engine.db.execute(
                        "INSERT OR REPLACE INTO appearance_references VALUES(?,?,?)",
                        (self.engine.owner, character_id, json.dumps(value)),
                    )
                else:
                    value = None
                    self.engine.db.execute(
                        "DELETE FROM appearance_references WHERE owner=? AND character_id=?",
                        (self.engine.owner, character_id),
                    )
        except Exception:
            if saved:
                self.path(saved).unlink(missing_ok=True)
            raise
        if previous:
            self.path(previous).unlink(missing_ok=True)
        return value

    def load_reference(self, character_id):
        with self.engine._lock:
            ref = self.reference(character_id)
            if not ref:
                return []
            image = ImageInput(path=self.path(ref)).load()
            if image.source_ref != ref["source_ref"] or image.mime != ref["mime"]:
                raise ValueError("Saved appearance reference changed; upload it again")
            return [image]

    def guidance(self, character_id):
        if not self.runtime.capabilities()["chat_images"]:
            return "\nImage generation is not enabled in this chat. image_request must be null."
        reference = self.reference(character_id)
        return (
            "\nImage generation is ENABLED for this chat. You can request ONE generated "
            "character image/selfie when the current user explicitly asks. "
            "Do not refuse solely because you are a virtual/fictional character. "
            "This is generated artwork, not a physical camera or a real-world event. "
            "Do not promise completion; the application displays the actual result or error. "
            "Use image_request=null for ordinary conversation, image analysis, quoted or "
            "hypothetical requests, and when the user says not to generate. "
            + (
                "A saved character reference image is available; the image model receives "
                "its pixels. Use it for identity rather than inventing hair/face details."
                if reference
                else "No reference image is saved. Use the character's appearance description. "
                "If it is also empty, ask the user to add an appearance or reference first."
            )
        )

    def execute(self, character_id, draft):
        """At most one image action per turn; text retries never silently buy another image."""
        from .media import Media

        turn_id = draft["turn_id"]
        with self.engine._lock:
            turn = self.engine._turn(character_id, turn_id)
            row = self.engine.db.execute(
                "SELECT payload FROM turn_drafts WHERE turn_id=?", (turn_id,)
            ).fetchone()
            draft = json.loads(row[0]) if row else dict(draft)
        request = CommitRequest.model_validate(draft["commit_request"]).image_request
        if request is None or "image_result" in draft:
            return draft

        def run():
            if not self.runtime.capabilities()["chat_images"]:
                return {"status": "unavailable", "error": "Image generation is not enabled."}
            if request.request_quote not in turn["user_message"]:
                return {"status": "failed", "error": "No matching image request in this message."}
            spec = json.loads(self.engine._character(character_id)["spec"])
            if not self.reference(character_id) and not spec.get("appearance", "").strip():
                return {
                    "status": "needs_appearance",
                    "error": "Add an appearance description or reference image "
                    "in character settings.",
                }
            try:
                result = Media(self.runtime).image(
                    character_id, request.prompt, "chat-image-" + turn_id
                )
                return {
                    "status": "succeeded",
                    "artifacts": [
                        {k: a[k] for k in ("artifact", "mime", "bytes")}
                        for a in result.get("artifacts", [result])
                    ],
                    "usage": result.get("usage", {}),
                }
            except (ValueError, OSError, RuntimeError):
                # The underlying image operation retains the failed/unknown provider receipt.
                # Surface failure, never turn a repeat chat request into an automatic retry.
                return {
                    "status": "failed",
                    "error": "The image could not be generated. It was not retried. "
                    "Check image settings before asking again.",
                }

        result = self.runtime.operation(
            character_id, "chat_image", turn_id, request.model_dump(), run
        )
        draft["image_result"] = result
        with self.engine._transaction():
            self.engine.db.execute(
                "UPDATE turn_drafts SET payload=? WHERE turn_id=?", (json.dumps(draft), turn_id)
            )
        return draft

    def history(self, character_id, **kwargs):
        turns = self.engine.history(character_id, **kwargs)
        with self.engine._lock:
            for turn in turns:
                row = self.engine.db.execute(
                    "SELECT payload FROM turn_drafts WHERE turn_id=?", (turn["id"],)
                ).fetchone()
                turn["image_result"] = json.loads(row[0]).get("image_result") if row else None
        return turns
