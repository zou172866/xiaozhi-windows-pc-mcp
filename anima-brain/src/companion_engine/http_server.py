"""Loopback-only JSON API and authenticated MCP HTTP, one trusted owner.
Not a public multi-tenant deployment. No CORS, no filesystem-path endpoints.
The optional settings-only app cannot chat or access character data.
"""

import base64
import json
import secrets
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute

from .engine import Conflict, NotFound
from .media import Media
from .models import CharacterSpec, MemoryDraft
from .provider_config import storage_status
from .providers import ProviderError
from .sdk_models import ImageInput, load_images


def create_app(
    runtime,
    token: str,
    *,
    port=8765,
    include_mcp=True,
    start_worker=True,
    settings_only=False,
    frontend_dir: Path | None = None,
    preview_mode=False,
):
    if len(token) < 24:
        raise ValueError("Local bearer token must contain at least 24 characters")
    if settings_only and (include_mcp or start_worker):
        raise ValueError("The settings-only app must not start MCP or a worker")
    if settings_only and frontend_dir is not None:
        raise ValueError("The settings-only app must not serve a frontend")
    frontend = Path(frontend_dir).resolve() if frontend_dir is not None else None
    if frontend is not None and not (frontend / "index.html").is_file():
        raise ValueError("Build the optional frontend first (missing index.html)")
    engine = runtime.engine
    media = Media(runtime)
    root = Path(__file__).parent / "static"
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def frontend_file(path):
        # Serve only the explicit build's entry point and static assets, never arbitrary
        # repo/configuration files or symlinks escaping the supplied directory.
        if frontend is None:
            return None
        relative = path.lstrip("/") or "index.html"
        if relative != "index.html" and not relative.startswith(("assets/", "portraits/")):
            return None
        candidate = (frontend / relative).resolve()
        allowed = {".html", ".js", ".css", ".woff2", ".woff", ".png", ".jpg", ".webp", ".svg"}
        if (
            candidate.is_relative_to(frontend)
            and candidate.suffix in allowed
            and candidate.is_file()
        ):
            return candidate
        return None

    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.headers.get("host") not in hosts:
                return JSONResponse({"error": "Invalid local host"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin not in {"http://" + h for h in hosts}:
                return JSONResponse({"error": "Cross-origin access denied"}, status_code=403)
            public = request.method in ("GET", "HEAD") and (
                frontend_file(request.url.path) is not None
                or (
                    settings_only
                    and request.url.path in ("/settings", "/settings.js", "/settings.css")
                )
            )
            if not public and not secrets.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + token
            ):
                return JSONResponse({"error": "A local bearer token is required"}, status_code=401)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; "
                + ("style-src 'self' 'unsafe-inline'; " if frontend else "style-src 'self'; ")
                + "img-src 'self' "
                "blob: data:; media-src 'self' blob:; "
                + (
                    "connect-src 'self' https: wss:; worker-src 'self' blob:; "
                    if frontend
                    else "connect-src 'self'; "
                )
                + "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
            return response

    async def frontend_asset(request):
        path = frontend_file(request.url.path)
        if path is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(path)

    async def static(request):
        file = {
            "/settings": "local-settings.html",
            "/settings.js": "local-settings.js",
            "/settings.css": "local-settings.css",
        }[request.url.path]
        return FileResponse(root / file)

    async def api(request: Request):
        try:
            body = {}
            if request.method == "POST":
                raw = bytearray()
                async for part in request.stream():
                    raw.extend(part)
                    if len(raw) > 28 * 1024 * 1024:
                        raise ValueError("Request exceeds 28MB")
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Expected JSON object")
            action = request.path_params["action"]
            if settings_only and action != "settings":
                return JSONResponse({"error": "Not found"}, status_code=404)
            character = body.get("character_id", request.query_params.get("character_id", ""))

            def dispatch():
                if request.method == "GET":
                    if action == "settings":
                        return {
                            "settings": runtime.settings.public(),
                            "credential_storage": storage_status(),
                        }
                    if action == "memory_panel":
                        return runtime.memory_panel(
                            character, int(request.query_params.get("offset", 0))
                        )
                    if action == "bootstrap":
                        return {
                            "characters": [c.model_dump() for c in engine.characters()],
                            "appearance_references": {
                                c.id: runtime.chat_images.reference(c.id)
                                for c in engine.characters()
                            },
                            "settings": runtime.settings.public(),
                            "credential_storage": storage_status(),
                            "mode": "local_single_owner",
                            "preview_mode": bool(preview_mode),
                            "capabilities": runtime.capabilities(),
                        }
                    if action == "turn":
                        return runtime.inspect_turn(character, request.query_params["turn_id"])
                    if action == "conversation":
                        return {
                            "turns": runtime.chat_images.history(character),
                            "pending": engine.pending_turn(character),
                            "memories": [
                                m.model_dump(mode="json") for m in engine.memories(character)
                            ],
                            "relationship": engine.relationship(character),
                            "core": engine.memory.documents(character),
                            "automation": runtime.automation.status(character),
                            "receipts": runtime.receipts(character),
                            "usage": engine.usage_summary(character),
                            "artifacts": runtime.artifacts(character),
                        }
                    if action == "history":
                        before = int(request.query_params["before"])
                        return {"turns": runtime.chat_images.history(character, before=before)}
                    if action == "appearance_reference":
                        return {"reference": runtime.chat_images.reference(character)}
                    if action == "export":
                        return engine.export_character(character)
                    raise ValueError("Unknown read operation")
                if action == "settings":
                    return runtime.configure_settings(
                        body.get("settings", {}),
                        clear_keys=body.get("clear_keys", []),
                        persist=body.get("persist", False),
                    )
                if action in ("create_character", "update_character"):
                    spec = CharacterSpec.model_validate(body["spec"])
                    reference = body.get("reference_image")
                    loaded = None
                    if reference is not None:
                        if not isinstance(reference, str):
                            raise ValueError("Reference image must be base64 data")
                        loaded = ImageInput(data=base64.b64decode(reference, validate=True)).load()

                    def save_character():
                        if action == "update_character" and engine.pending_turn(spec.id):
                            raise Conflict(
                                "Finish or discard the pending turn before editing character"
                            )
                        return (
                            runtime.create_character(spec)
                            if action == "create_character"
                            else engine.update_character(spec)
                        )

                    if "reference_image" in body:
                        # Keep character + file metadata atomic; old bytes are removed only
                        # after the outer transaction commits, not after a nested savepoint.
                        runtime.chat_images.set_reference(
                            spec.id, loaded, save_character=save_character
                        )
                    else:
                        with engine._transaction():
                            save_character()
                    return spec.model_dump()
                if action == "configure_memory":
                    return runtime.automation.configure(character, body["policy"])
                if action == "retry_memory_job":
                    return runtime.automation.acknowledge(character, body["job_id"])
                if action == "chat":
                    attachments = body.get("attachments", [])
                    if not isinstance(attachments, list) or len(attachments) > 4:
                        raise ValueError("Supply at most four image attachments")
                    images = []
                    for attachment in attachments:
                        if not isinstance(attachment, dict) or set(attachment) - {"data", "detail"}:
                            raise ValueError("Attachments accept base64 data and detail only")
                        images.append(
                            ImageInput(
                                data=base64.b64decode(attachment["data"], validate=True),
                                detail=attachment.get("detail", "auto"),
                            )
                        )
                    return runtime.chat(
                        character,
                        body["message"],
                        body["request_id"],
                        attachments=load_images(images),
                    )
                if action == "approve":
                    if not isinstance(body["retain"], bool):
                        raise ValueError("retain must be boolean")
                    return runtime.approve(character, body["turn_id"], retain=body["retain"])
                if action == "abort":
                    engine.abort_turn(character, body["turn_id"])
                    return {"aborted": True}
                if action == "correct_memory":
                    return engine.correct_memory(
                        character,
                        body["memory_id"],
                        MemoryDraft.model_validate(body["draft"]),
                        expected_revision=body["expected_revision"],
                    ).model_dump(mode="json")
                if action == "delete_memory":
                    engine.delete_memory(
                        character, body["memory_id"], expected_revision=body["expected_revision"]
                    )
                    return {"deleted": True, "source_transcript_deleted": False}
                if action == "recall":
                    return engine.memory.recall(character, body["query"])
                if action == "maintain":
                    return runtime.maintain(character, body["request_id"])
                if action == "index":
                    return runtime.index(character, body["request_id"])
                if action == "speech":
                    return media.speech(character, body["text"], body["request_id"])
                if action == "image":
                    return media.image(character, body["prompt"], body["request_id"])
                if action == "transcribe":
                    audio = base64.b64decode(body["audio"], validate=True)
                    return media.transcribe(character, audio, body["mime"], body["request_id"])
                raise ValueError("Unknown operation")

            return JSONResponse(await run_in_threadpool(dispatch))
        except ValidationError:
            return JSONResponse(
                {"error": "Invalid structured input; check fields and bounds"}, status_code=422
            )
        except (Conflict, NotFound) as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=409 if isinstance(exc, Conflict) else 404
            )
        except ProviderError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except (ValueError, KeyError, TypeError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                else "Invalid request"
            )
            return JSONResponse({"error": message}, status_code=400)

    async def artifact(request):
        try:
            path, mime = media.artifact(
                request.query_params.get("character_id", ""), request.path_params["name"]
            )
            return FileResponse(path, media_type=mime)
        except (ValueError, OSError):
            return JSONResponse({"error": "Artifact not found"}, status_code=404)

    routes = [Route("/api/{action}", api, methods=["GET", "POST"])]
    if settings_only:
        routes.extend(
            Route(path, static) for path in ("/settings", "/settings.js", "/settings.css")
        )
    else:
        routes.append(Route("/media/{name}", artifact))
        from .call_http import call_endpoint

        routes.append(WebSocketRoute("/api/call", call_endpoint(runtime, token, hosts)))
    mcp_app = None
    if include_mcp:
        from .mcp_server import create_server

        mcp_app = create_server(engine, runtime).streamable_http_app(
            stateless_http=True, json_response=True
        )
        routes.extend(mcp_app.routes)
    if frontend:
        routes.append(Route("/{path:path}", frontend_asset))

    @asynccontextmanager
    async def lifespan(app):
        with runtime.automation.worker() if start_worker else nullcontext():
            if mcp_app:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(Guard)
    return app
