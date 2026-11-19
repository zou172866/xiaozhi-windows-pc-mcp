"""Local MCP Apps memory UI QA with disposable synthetic data and no provider calls."""

import argparse
import asyncio
import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from player_preview import HOST_HTML

from companion_engine import CharacterSpec, CommitRequest, Engine, MemoryDraft
from companion_engine.mcp_server import create_server
from companion_engine.runtime import Runtime, Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=49201)
    parser.add_argument("--component", choices=["memory", "settings"], default="memory")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="companion-memory-qa-") as directory:
        with Engine(Path(directory) / "preview.sqlite3") as engine:
            engine.create_character(CharacterSpec(id="nova", name="Nova", persona="Synthetic QA"))
            ctx = engine.prepare_turn("nova", "I like tea. My dog is Mochi.", request_id="fixture")
            engine.commit_turn(
                "nova",
                ctx.turn_id,
                CommitRequest(
                    assistant_reply="Noted.",
                    memories=[
                        MemoryDraft(key="drink", text="Prefers tea.", source_quote="I like tea."),
                        MemoryDraft(
                            key="pet",
                            text="Has a dog named Mochi.",
                            source_quote="My dog is Mochi.",
                        ),
                    ],
                ),
            )
            engine.memory.consolidate(
                "nova", "core", "Prefers tea and has a dog named Mochi.", [ctx.turn_id + ":user"]
            )
            runtime = Runtime(engine, Settings())
            server = create_server(engine, runtime)
            html = (
                HOST_HTML.replace("voice note", "memory panel")
                .replace("Voice message player", "Memory manager")
                .replace("height:340px", "height:800px")
                .replace("SYNTHETIC AUDIO ONLY", "DISPOSABLE SYNTHETIC MEMORIES")
                .replace(
                    "Playback has no API cost.",
                    "Edits affect only a disposable test database. No API cost.",
                )
                .replace("Audio reads:", "Tool calls:")
            )

            class Handler(BaseHTTPRequestHandler):
                def reply(self, data, mime="application/json", code=200):
                    body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    if self.path == "/":
                        self.reply(html, "text/html; charset=utf-8")
                    elif self.path == "/player":
                        self.reply(
                            (
                                Path(__file__).resolve().parents[1]
                                / "src/companion_engine/static"
                                / (
                                    "memory-panel.html"
                                    if args.component == "memory"
                                    else "provider-settings.html"
                                )
                            ).read_text(),
                            "text/html; charset=utf-8",
                        )
                    elif self.path == "/card":
                        from companion_engine.settings_ui import provider_card

                        self.reply(
                            {
                                "structuredContent": runtime.memory_panel("nova")
                                if args.component == "memory"
                                else provider_card(runtime)
                            }
                        )
                    else:
                        self.reply({}, code=404)

                def do_POST(self):
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if size > 20000:
                            raise ValueError("Too large")
                        body = json.loads(self.rfile.read(size))
                        if body["name"] not in {
                            "refresh_memory_panel",
                            "edit_memory_from_panel",
                            "delete_memory_from_panel",
                            "expand_evidence",
                            "refresh_provider_settings",
                        }:
                            raise ValueError("Not a test tool")
                        result = asyncio.run(server.call_tool(body["name"], body["arguments"]))
                        self.reply(result.model_dump(mode="json", by_alias=True, exclude_none=True))
                    except Exception:
                        self.reply(
                            {"isError": True, "content": [{"type": "text", "text": "Test error"}]}
                        )

                def log_message(self, *_):
                    pass

            print(f"Local synthetic memory preview: http://127.0.0.1:{args.port}/", flush=True)
            ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
