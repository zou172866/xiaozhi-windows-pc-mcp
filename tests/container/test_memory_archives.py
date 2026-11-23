"""Exercise the first import, connection handoff and replay using disposable state."""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get("XIAOZHI_APP_DIR", str(Path(__file__).resolve().parents[2] / "app")))
import memory_archives as archives


def export(key="a", index=1, user="明天去旧书店", reply="好，在门口见"):
    return {"format": "xiaozhi-tavern-v1", "chat_key": key * 64,
            "turns": [{"index": index, "user_message": user, "assistant_reply": reply}]}


class TestArchives(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enter_state = patch.object(archives.xz_common, "STATE_DIR", temp.name)
        self.enter_state.start()
        self.addCleanup(self.enter_state.stop)
        self.enqueue = Mock()

    def test_archive_choice_survives_reopen_and_preserves_daily_identity(self):
        self.assertEqual(archives.selected_binding("desk"), ("daily", "", (), -1))
        self.assertEqual(archives.scoped_character_id("original", "daily", "desk"), "original")
        archives.import_handoff("Desk", export(), self.enqueue)
        story = archives.selected_binding("desk")
        self.assertEqual(story, ("tavern", "a" * 64, (("明天去旧书店", "好，在门口见"),), 1))
        self.assertEqual(archives.chosen_archive("bedroom"), "daily")
        archives.select_archive("desk", "daily")
        self.assertEqual(archives.selected_binding("desk"), ("daily", "", (), -1))
        archives.select_archive("DESK", "tavern")
        self.assertEqual(archives.selected_binding("desk"), story)
        cid = self.enqueue.call_args.kwargs["character_id"]
        self.assertNotEqual(cid, archives.scoped_character_id(None, "tavern", "bedroom", story[1]))
        archives.import_handoff("desk", export(key="b"), self.enqueue)
        self.assertNotEqual(cid, self.enqueue.call_args.kwargs["character_id"])

    def test_rollback_after_outbox_failure_and_idempotent_retry(self):
        retry = Mock(side_effect=[OSError("unavailable"), None])
        with self.assertRaises(OSError):
            archives.import_handoff("desk", export(), retry)
        self.assertEqual(archives.chosen_archive("desk"), "daily")
        self.assertEqual(archives.import_handoff("desk", export(), retry), 1)
        self.assertEqual(retry.call_args_list[0], retry.call_args_list[1])
        self.assertEqual(archives.import_handoff("desk", export(), retry), 0)
        self.assertEqual(retry.call_count, 2)

    def test_late_ack_and_conflicting_versions_do_not_overwrite_new_progress(self):
        archives.import_handoff("desk", export(), self.enqueue)
        _, key, turns, old_idx = archives.selected_binding("desk")
        archives.import_handoff("desk", export(index=3), self.enqueue)
        archives.acknowledge_handoff("desk", key, turns, old_idx)
        self.assertEqual(archives.selected_binding("desk")[2:], (turns, 3))
        for stale in (export(), export(index=3, reply="另一个结局")):
            with self.assertRaises(archives.HandoffConflict):
                archives.import_handoff("desk", stale, self.enqueue)
        archives.acknowledge_handoff("desk", key, turns, 3)
        archives.import_handoff("desk", export(index=3), self.enqueue)
        self.assertEqual(archives.selected_binding("desk")[2:], ((), -1))

    def test_malformed_state_and_invalid_input_do_not_select_or_deliver(self):
        for bad in (export(reply=""), export(index=True), export(key="X"), export(user="a" * 2001)):
            with self.assertRaises(ValueError):
                archives.import_handoff("desk", bad, self.enqueue)
        self.enqueue.assert_not_called()
        Path(archives._path()).write_bytes(b"corrupt fixture")
        with self.assertRaises(sqlite3.DatabaseError):
            archives.selected_binding("desk")


if __name__ == "__main__":
    unittest.main()
