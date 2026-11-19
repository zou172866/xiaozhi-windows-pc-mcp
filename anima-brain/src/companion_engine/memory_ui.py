"""Inspectable MCP Apps memory UI backed by revision-checked engine operations."""

from pathlib import Path

from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .models import MemoryDraft


def result(data):
    return CallToolResult(
        content=[TextContent(type="text", text="Memory panel updated.")],
        structured_content=data,
    )


MEMORY_URI = "ui://companion-engine/memory-panel-v1.html"


def register_memory_ui(server, runtime):
    engine = runtime.engine
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    destructive = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

    @server.resource(
        MEMORY_URI,
        mime_type="text/html;profile=mcp-app",
        meta={
            "ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}},
            "openai/widgetPrefersBorder": False,
            "openai/widgetDescription": "Inspect facts, cited summaries and indexing status.",
            "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
        },
    )
    def memory_panel_html() -> str:
        return (Path(__file__).parent / "static/memory-panel.html").read_text(encoding="utf-8")

    @server.tool(
        annotations=read,
        meta={
            "ui": {"resourceUri": MEMORY_URI},
            "openai/outputTemplate": MEMORY_URI,
        },
    )
    def show_memory_panel(character_id: str) -> CallToolResult:
        """Show the real memory manager: facts, recent cited summaries and maintenance status."""
        return result(runtime.memory_panel(character_id))

    @server.tool(
        annotations=read,
        meta={
            "ui": {"visibility": ["app"]},
            "openai/widgetAccessible": True,
        },
    )
    def refresh_memory_panel(character_id: str, offset: int = 0) -> CallToolResult:
        """UI-only read; paginate current facts in groups of 50."""
        return result(runtime.memory_panel(character_id, offset))

    @server.tool(
        annotations=destructive,
        meta={
            "ui": {"visibility": ["app"]},
            "openai/widgetAccessible": True,
        },
    )
    def edit_memory_from_panel(
        character_id: str, memory_id: str, expected_revision: int, text: str
    ) -> CallToolResult:
        """Save the user's explicitly edited assertion; stale revisions fail, never overwrite."""
        with engine._lock:
            current = engine._memory(engine._get_memory(character_id, memory_id))
        draft = MemoryDraft(
            key=current.key,
            category=current.category,
            text=text,
            source_quote=text,
            details=current.details,
        )
        engine.correct_memory(character_id, memory_id, draft, expected_revision=expected_revision)
        return result({"saved": True, "memory_id": memory_id})

    @server.tool(
        annotations=destructive,
        meta={
            "ui": {"visibility": ["app"]},
            "openai/widgetAccessible": True,
        },
    )
    def delete_memory_from_panel(
        character_id: str, memory_id: str, expected_revision: int
    ) -> CallToolResult:
        """Delete a confirmed fact/revisions and cached summaries; original chat remains."""
        engine.delete_memory(character_id, memory_id, expected_revision=expected_revision)
        return result({"deleted": True, "memory_id": memory_id})
