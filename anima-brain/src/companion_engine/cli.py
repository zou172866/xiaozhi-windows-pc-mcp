import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from .engine import Engine
from .models import CharacterSpec, CommitRequest, MemoryDraft


def emit(value):
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=lambda v: v.model_dump() if isinstance(v, BaseModel) else str(v),
        )
    )


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parser():
    root = argparse.ArgumentParser(
        description="Local-first companion engine; JSON in/out, configured memory automation"
    )
    root.add_argument(
        "--db",
        default=os.environ.get("COMPANION_DB", "~/.local/share/companion-engine/brain.sqlite3"),
    )
    root.add_argument("--owner", default=os.environ.get("COMPANION_OWNER", "local"))
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize a local database (no network)")
    sub.add_parser("list", help="List local characters")
    sub.add_parser("worker", help="Run configured durable memory maintenance until interrupted")
    sub.add_parser("serve", help="Run the local stdio MCP server (no network listener)")
    http = sub.add_parser("http", help="Run private loopback JSON API and MCP HTTP (no chat UI)")
    http.add_argument("--port", type=int, default=8765)
    sub.add_parser("settings", help="Open only the private local provider-settings form")
    sub.add_parser(
        "mcp-config", help="Print a stdio client configuration; does not edit client settings"
    )
    sub.add_parser("doctor", help="Check local installation/database, without API requests")
    package = sub.add_parser("plugin-package", help="Export skill-only plugin; no secrets or DB")
    package.add_argument("output", help="New .zip path; existing files are never overwritten")
    package.add_argument("--app-id", help="Optional existing registered MCP app ID")
    hosts = sub.add_parser("host-package", help="Export Claude and DeepSeek Harness local setup")
    hosts.add_argument("output", help="New directory; never overwrites existing exports")
    hosts.add_argument(
        "--settings-file", help="Optional private provider settings path (not copied)"
    )
    create = sub.add_parser("create")
    create.add_argument("character")
    create.add_argument("--name", required=True)
    group = create.add_mutually_exclusive_group(required=True)
    group.add_argument("--persona")
    group.add_argument("--persona-file")
    create.add_argument("--relationship", action=argparse.BooleanOptionalAction, default=True)
    create.add_argument("--romance", action="store_true")
    prepare = sub.add_parser("prepare", help="Record a pending user turn; retrieve context")
    prepare.add_argument("character")
    prepare.add_argument("message")
    prepare.add_argument("--request-id", default=None)
    for name in ("pending", "export", "draft", "usage"):
        sub.add_parser(name).add_argument("character")
    for name in ("commit", "abort"):
        command = sub.add_parser(name)
        command.add_argument("character")
        command.add_argument("turn_id")
        if name == "commit":
            command.add_argument("--file", required=True, help="CommitRequest JSON file")
    forget = sub.add_parser("forget-character")
    forget.add_argument("character")
    forget.add_argument("--confirm", required=True)
    mem = sub.add_parser("memory")
    ops = mem.add_subparsers(dest="operation", required=True)
    for name in ("list", "search", "remember", "correct", "delete"):
        command = ops.add_parser(name)
        command.add_argument("character")
        if name == "list":
            command.add_argument("--offset", type=int, default=0)
        if name == "search":
            command.add_argument("query")
        if name in ("remember", "correct"):
            command.add_argument("--file", required=True, help="MemoryDraft JSON file")
        if name in ("correct", "delete"):
            command.add_argument("memory_id")
            command.add_argument("--revision", type=int, required=True)
    return root


def run(args):
    if args.command in {"host-package", "mcp-config"}:
        from .hosts import export_hosts, mcp_config

        if args.command == "mcp-config":
            emit(mcp_config(args.db, args.owner))
        else:
            emit(export_hosts(args.output, args.db, args.owner, settings_file=args.settings_file))
        return
    if args.command == "plugin-package":
        import re
        import zipfile

        if args.app_id and not re.fullmatch(r"asdk_app_[A-Za-z0-9_-]+", args.app_id):
            raise ValueError("Expected an existing registered MCP app ID, not a URL or credential")
        root = Path(__file__).parent / "plugin"
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
        if args.app_id:
            manifest["extensions"] = {"com.openai": {"apps": "./.app.json"}}
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("plugin.json", json.dumps(manifest, indent=2))
            for path in sorted((root / "skills").rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(root))
            if args.app_id:
                archive.writestr(
                    ".app.json", json.dumps({"apps": {"companion_engine": {"id": args.app_id}}})
                )
        emit({"package": str(output.absolute()), "registered_mcp_mapping": bool(args.app_id)})
        return
    with Engine(args.db, owner=args.owner) as engine:
        if args.command == "settings":
            import threading

            from .runtime import Runtime

            runtime = Runtime(engine)
            try:
                emit(runtime.local_settings.open())
                print("Settings open on this computer. Ctrl-C to close.", flush=True)
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            finally:
                runtime.local_settings.close()
            return
        if args.command == "http":
            import secrets

            try:
                import uvicorn

                from .http_server import create_app
            except ImportError:
                raise ValueError('Install HTTP support: pip install -e ".[http]"') from None

            from .runtime import Runtime

            if not 1024 <= args.port <= 65535:
                raise ValueError("port must be 1024..65535")
            token = os.environ.get("COMPANION_LOCAL_TOKEN") or secrets.token_urlsafe(32)
            print(f"Local MCP endpoint: http://127.0.0.1:{args.port}/mcp", flush=True)
            print("Private Authorization header (do not share): Bearer " + token, flush=True)
            runtime = Runtime(engine)
            try:
                uvicorn.run(
                    create_app(runtime, token, port=args.port),
                    host="127.0.0.1",
                    port=args.port,
                    access_log=False,
                )
            finally:
                runtime.local_settings.close()
            return
        if args.command == "serve":
            try:
                from .mcp_server import create_server
            except ImportError:
                raise ValueError('Install MCP support: pip install -e ".[mcp]"') from None
            from .runtime import Runtime

            runtime = Runtime(engine)
            try:
                with runtime.automation.worker():
                    create_server(engine, runtime).run(transport="stdio")
            finally:
                runtime.local_settings.close()
            return
        if args.command == "worker":
            import threading

            from .runtime import Runtime

            try:
                with Runtime(engine).automation.worker():
                    threading.Event().wait()
            except KeyboardInterrupt:
                pass
            return
        if args.command == "init":
            emit({"database": str(engine.database), "owner": engine.owner, "schema": 2})
        elif args.command == "doctor":
            emit(
                {
                    "python": sys.version.split()[0],
                    "database": str(engine.database),
                    "schema": engine.db.execute("PRAGMA user_version").fetchone()[0],
                    "integrity": engine.db.execute("PRAGMA quick_check").fetchone()[0],
                    "fts5": True,
                    "mcp_installed": importlib.util.find_spec("mcp") is not None,
                    "database_mode": oct(engine.database.stat().st_mode & 0o777),
                    "network_requests": 0,
                }
            )
        elif args.command == "list":
            emit(engine.characters())
        elif args.command == "create":
            persona = (
                Path(args.persona_file).read_text(encoding="utf-8")
                if args.persona_file
                else args.persona
            )
            emit(
                engine.create_character(
                    CharacterSpec(
                        id=args.character,
                        name=args.name,
                        persona=persona,
                        relationship_enabled=args.relationship,
                        romance_enabled=args.romance,
                    )
                )
            )
        elif args.command == "prepare":
            request_id = args.request_id or str(uuid4())
            emit(engine.prepare_turn(args.character, args.message, request_id=request_id))
        elif args.command == "pending":
            emit(engine.pending_turn(args.character))
        elif args.command == "commit":
            emit(
                engine.commit_turn(
                    args.character, args.turn_id, CommitRequest.model_validate(read_json(args.file))
                )
            )
        elif args.command == "abort":
            engine.abort_turn(args.character, args.turn_id)
            emit({"aborted": True})
        elif args.command == "export":
            emit(engine.export_character(args.character))
        elif args.command == "usage":
            emit(engine.usage_summary(args.character))
        elif args.command == "forget-character":
            engine.forget_character(args.character, confirmation=args.confirm)
            emit({"forgotten": True})
        elif args.command == "draft":
            from .generation import draft_reply
            from .providers import CompatibleProvider

            if not os.environ.get("COMPANION_MODEL") or not os.environ.get("COMPANION_BASE_URL"):
                raise ValueError(
                    "Set COMPANION_MODEL and COMPANION_BASE_URL; API key is COMPANION_API_KEY"
                )

            def price(name):
                return float(os.environ[name]) if os.environ.get(name) else None

            provider = CompatibleProvider(
                base_url=os.environ["COMPANION_BASE_URL"],
                model=os.environ["COMPANION_MODEL"],
                api_key=os.environ.get("COMPANION_API_KEY"),
                input_usd_per_million=price("COMPANION_INPUT_USD_PER_MILLION"),
                output_usd_per_million=price("COMPANION_OUTPUT_USD_PER_MILLION"),
            )
            emit(draft_reply(engine, args.character, provider))
        elif args.command == "memory":
            if args.operation == "list":
                emit(engine.memories(args.character, offset=args.offset))
            elif args.operation == "search":
                emit(engine.search(args.character, args.query))
            elif args.operation == "remember":
                emit(
                    engine.remember(
                        args.character, MemoryDraft.model_validate(read_json(args.file))
                    )
                )
            elif args.operation == "correct":
                emit(
                    engine.correct_memory(
                        args.character,
                        args.memory_id,
                        MemoryDraft.model_validate(read_json(args.file)),
                        expected_revision=args.revision,
                    )
                )
            elif args.operation == "delete":
                engine.delete_memory(
                    args.character, args.memory_id, expected_revision=args.revision
                )
                emit({"deleted": True, "source_transcript_deleted": False})


def main():
    args = parser().parse_args()
    try:
        run(args)
    except (ValueError, OSError, RuntimeError) as exc:
        # Pydantic errors can include private input; do not echo model output/credentials.
        from pydantic import ValidationError

        message = (
            "Invalid structured input; check required fields and bounds"
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        print(
            json.dumps({"error": type(exc).__name__, "message": message}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
