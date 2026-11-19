"""Explicit opt-in credential entry for a private owner-controlled MCP connection."""

import os

from mcp.types import CallToolResult, TextContent, ToolAnnotations

PRIVATE_CONFIG_INSTRUCTIONS = """Private owner-enabled credential configuration is available.
If the user explicitly asks to save/change provider credentials through chat, use
configure_private_provider directly. Do not pass credential-bearing messages to prepare_turn,
commit_turn, memories, summaries, voice generation or image tools. Never repeat the key.
This saves on the engine computer, not in ChatGPT's memory. The chat/tool invocation still
contains the submitted key; do not claim it is hidden from the host or removed from history.
Use only the service, endpoint and model the user chose or already configured; do not guess
a destination for credentials. Saving does not test a provider or authorize paid requests.
For this private connection this credential route supersedes the packaged workflow's local-only
key-entry guidance. All other memory/privacy rules remain in effect.
"""


def enabled():
    return os.environ.get("COMPANION_ALLOW_CHAT_CREDENTIALS") == "1"


def register_private_config(server, runtime):
    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def configure_private_provider(service: str, settings: dict) -> CallToolResult:
        """Save explicitly supplied provider settings on the engine computer and apply now.

        Private opt-in only. service: chat, embedding, speech, image or transcription.
        settings accepts api_key, model, base_url; speech accepts api_key, model, provider
        (elevenlabs, minimax or doubao), not base_url. Omitted/blank keys stay unchanged.
        No provider request, connectivity test, voice change or paid-job activation is made.
        Never send this input to conversation/memory tools. Never echo the key in your reply.
        """
        from .provider_config import storage_status

        prefixes = {
            "chat": "",
            "embedding": "embedding_",
            "speech": "speech_",
            "image": "image_",
            "transcription": "transcription_",
        }
        try:
            if service not in prefixes or not settings:
                raise ValueError()
            allowed = {"api_key", "model", "provider" if service == "speech" else "base_url"}
            if set(settings) - allowed or any(
                not isinstance(v, str) or len(v) > 8192 for v in settings.values()
            ):
                raise ValueError()
            prefix = prefixes[service]
            patch = {
                prefix + ("key" if k == "api_key" and prefix else k): v for k, v in settings.items()
            }
            runtime.configure_settings(patch, persist=True)
            # Only selected booleans; no endpoints or other user-controlled values echoed.
            key_name = prefix + "key" if prefix else "api_key"
            status = {
                "saved": True,
                "applied_to_running_engine": True,
                "key_configured": bool(getattr(runtime.settings, key_name).get_secret_value()),
                "storage": storage_status()["saved_secrets"],
                "provider_tested": False,
            }
            return CallToolResult(
                content=[TextContent(type="text", text="Provider settings saved and applied.")],
                structured_content=status,
            )
        except Exception:
            # Return a sanitized result rather than logging an exception/traceback.
            return CallToolResult(
                is_error=True,
                content=[
                    TextContent(
                        type="text",
                        text="Provider configuration failed. Check service/settings or "
                        "local secret storage; no provider request was made.",
                    )
                ],
            )
