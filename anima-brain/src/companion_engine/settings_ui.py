"""Native-style status card. Secret input remains outside the ChatGPT iframe."""

from pathlib import Path

from mcp.types import CallToolResult, TextContent, ToolAnnotations

SETTINGS_URI = "ui://companion-engine/provider-settings-v1.html"


def provider_card(runtime):
    from .provider_config import storage_status

    s = runtime.settings
    return {
        "speech": bool(s.speech_model and s.speech_key.get_secret_value()),
        "embedding": bool(s.embedding_base_url and s.embedding_model),
        "images": bool(s.image_base_url and s.image_model),
        "reply_model": bool(s.base_url and s.model),
        "storage": storage_status()["saved_secrets"],
        "opens_on": "engine_computer",
    }


def register_settings_ui(server, runtime):
    def result(data):
        return CallToolResult(
            content=[TextContent(type="text", text="Provider connection status.")],
            structured_content=data,
        )

    @server.resource(
        SETTINGS_URI,
        mime_type="text/html;profile=mcp-app",
        meta={
            "ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}},
            "openai/widgetPrefersBorder": False,
            "openai/widgetCSP": {"connect_domains": [], "resource_domains": []},
        },
    )
    def settings_html():
        return (Path(__file__).parent / "static/provider-settings.html").read_text()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        meta={
            "ui": {"resourceUri": SETTINGS_URI},
            "openai/outputTemplate": SETTINGS_URI,
        },
    )
    def show_provider_settings() -> CallToolResult:
        """Show provider connection status and a local settings launcher. No secret fields."""
        return result(provider_card(runtime))

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        meta={
            "ui": {"visibility": ["app"]},
            "openai/widgetAccessible": True,
        },
    )
    def refresh_provider_settings() -> CallToolResult:
        """Refresh connection status for the settings card; never return credentials."""
        return result(provider_card(runtime))

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        meta={
            "ui": {"visibility": ["app"]},
            "openai/widgetAccessible": True,
        },
    )
    def open_local_provider_settings() -> CallToolResult:
        """After the user's button click, open private settings on the engine's computer.

        No API keys or local authorization token travel through ChatGPT.
        This opens the computer's browser even if ChatGPT is being used on a phone.
        """
        return result(runtime.local_settings.open())
