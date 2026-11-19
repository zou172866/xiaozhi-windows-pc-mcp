"""Opt-in real provider smoke: two calls, restart recall, then an offline exact replay.

Uses only synthetic messages in a disposable database. Requires COMPANION_BASE_URL,
COMPANION_MODEL and provider credentials configured through Settings.from_env().
Run: python examples/live_smoke.py --output ./output/live-smoke.json
"""

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from companion_engine import BrainConfig, CompanionBrain, Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path; prior smoke results are never overwritten")
    settings = Settings.from_env()
    if not settings.base_url or not settings.model:
        parser.error("Configure a real provider first; no fallback model is selected")
    settings.reader_max_steps = 0
    config = BrainConfig(recent_turns=0, capture_context=True, start_worker=False)
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "model": settings.model,
        "synthetic_messages_only": True,
        "max_reply_calls": 2,
        "checks": {},
        "turns": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="companion-smoke-") as temporary:
            db = Path(temporary) / "brain.sqlite3"
            with CompanionBrain(db, settings=settings, config=config) as brain:
                brain.create_character(
                    character_id="mika",
                    name="Mika",
                    persona="Warm, direct, concise.",
                    relationship_enabled=False,
                )
                first = brain.chat_sync(
                    "mika",
                    "Please remember: my favorite tea is jasmine.",
                    request_id="write",
                )
                report["turns"].append(first.model_dump(mode="json"))
                report["checks"]["fact_saved"] = any(
                    "jasmine" in m.text.lower() for m in brain.memories("mika")
                )
            with CompanionBrain(db, settings=settings, config=config) as brain:
                recalled = brain.chat_sync("mika", "What is my favorite tea?", request_id="read")
                report["turns"].append(recalled.model_dump(mode="json"))
                report["checks"].update(
                    restart_recall="jasmine" in recalled.text.lower(),
                    recent_history_disabled=recalled.generation["recent_turns"] == 0,
                    evidence_delivered=bool(
                        recalled.generation["memory_ids"] or recalled.generation["evidence_ids"]
                    ),
                )
            # No settings/provider: this must succeed without a new model call.
            with CompanionBrain(db, config=config) as brain:
                replay = brain.chat_sync("mika", "What is my favorite tea?", request_id="read")
                report["checks"]["offline_replay"] = (
                    replay.replayed and replay.text == recalled.text
                )
        report["passed"] = all(report["checks"].values())
    except Exception as exc:
        report["passed"] = False
        report["error_type"] = type(exc).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "turns"}, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
