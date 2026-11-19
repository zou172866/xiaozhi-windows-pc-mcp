#!/usr/bin/env python3
"""Serve the separately built showcase on loopback, using the real Python brain.

Run from the repository root. No provider is called on startup. --demo is explicitly
scripted, uses its own database, and does not load provider credentials.
"""

import argparse
import secrets
import threading
import webbrowser
from pathlib import Path

import uvicorn

from companion_engine import BrainConfig, CompanionBrain, Settings
from companion_engine.http_server import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--storage", type=Path, help="SQLite path (default: output/showcase/brain.sqlite3)"
    )
    parser.add_argument(
        "--open", action="store_true", help="Open the private local URL in a browser"
    )
    parser.add_argument(
        "--demo", action="store_true", help="Scripted offline preview; no provider calls"
    )
    parser.add_argument(
        "--adaptive-recall",
        action="store_true",
        help="Allow bounded provider-assisted memory lookups (additional model calls)",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a port between 1024 and 65535")
    root = Path(__file__).resolve().parent
    if not (root / "dist" / "index.html").is_file():
        parser.error(
            "Build first: npm ci --prefix examples/showcase "
            "&& npm run build --prefix examples/showcase"
        )
    provider = None
    settings = Settings() if args.demo else Settings.from_env()
    settings.reader_enabled = args.adaptive_recall
    if args.demo:
        from demo import DemoProvider

        provider = DemoProvider()
    storage = args.storage or Path(
        "output/showcase/preview.sqlite3" if args.demo else "output/showcase/brain.sqlite3"
    )
    storage.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{args.port}/#token={token}"
    with CompanionBrain(
        storage,
        settings=settings,
        provider=provider,
        config=BrainConfig(capture_context=True, start_worker=False, allow_chat_images=True),
    ) as brain:
        if args.demo:
            from demo import seed

            seed(brain)
        app = create_app(
            brain.runtime,
            token,
            port=args.port,
            include_mcp=False,
            start_worker=False,
            frontend_dir=root / "dist",
            preview_mode=args.demo,
        )
        print(
            "\nCompanion Studio" + (" · OFFLINE SCRIPTED PREVIEW" if args.demo else ""), flush=True
        )
        print(f"Storage: {storage.resolve()}", flush=True)
        print(f"Private link (do not share): {url}\n", flush=True)
        if args.open:
            timer = threading.Timer(1, lambda: webbrowser.open(url))
            timer.daemon = True
            timer.start()
        uvicorn.run(
            app, host="127.0.0.1", port=args.port, access_log=False, ws_max_size=1024 * 1024
        )


if __name__ == "__main__":
    main()
