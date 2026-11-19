# Connection modes

Anima Brain retains the `companion-engine` host/plugin identifiers for compatibility.
The dated ChatGPT observations below describe the maintainer's local verification;
cloning this repository does not install a plugin into your account or share that connection.
Create your own host configuration with the [host integration guide](host-integrations.md).

## Local agent: use its own model

Install `.[mcp]` or `.[http]`, then `companion --db ./brain.sqlite3 mcp-config`.
Generated JSON contains absolute executable/database paths and `serve` arguments. Adapt the
outer configuration format to the client; no actual client settings are modified. Codex TOML:

```toml
[mcp_servers.companion]
command = "/absolute/path/companion-engine/.venv/bin/companion"
args = ["--db", "/absolute/path/brain.sqlite3", "serve"]
```

Reload tools as the client requires. List/create → prepare → retrieve if needed → generate with
host → commit. The host can write grounded contextual notes, summaries and appraisals directly,
without another chat API. Optional embeddings/media are paid tools requiring deliberate use.
The host must follow the protocol: MCP cannot capture unrelated conversations or guarantee writes.

## Local HTTP API / MCP

`companion --db ./brain.sqlite3 http` serves the JSON API and MCP against one engine/database.
It does not open a browser or serve a chat page. The CLI prints a private bearer token.
HTTP MCP: `http://127.0.0.1:8765/mcp`, `Authorization: Bearer <launch-token>`.
Use an MCP-compatible Accept header. Token rotates on restart unless explicitly configured.
Loopback, Host/Origin validation and one configured owner are intentional. Do not expose the port.

## ChatGPT

Official docs support MCP/plugin connections, including a private secure-tunnel development path.
A cloud host cannot directly reach laptop localhost. On 2026-10-30, a private ChatGPT plugin
was connected through Secure MCP Tunnel to this installation's stdio server. ChatGPT discovered
all 25 tools and successfully called `list_characters` once, returning `{"result": []}`.
Local verification also found zero characters, turns and memories. This was a read-only
connection smoke test, not a test of memory persistence, emotional behavior or paid providers.
No public plugin was published; another user must configure their own account/workspace connection.

To use the installed private plugin, open a new ChatGPT chat, select **Companion Engine** with
`@`, and ask it to list or create a character. Character creation and subsequent memory writes
must be intentional. ChatGPT supplies the response model; this engine supplies the tools/state.
Only turns actually sent to the tools are stored, not every ChatGPT conversation automatically.

Version 0.2.1 adds a packaged companion skill and `get_companion_workflow`, `provider_status`,
`show_voice_message`, and app-only `read_voice_chunk`. An already-running stdio process does
not hot-reload Python changes. Restart it safely with its existing runtime credential available,
then refresh/reconnect tool discovery in the host. Do not rotate or widen permissions just to
refresh tools. On 2026-11-05 the private ChatGPT connection was refreshed and its tool detail
dialog showed all 29 tools. Existing chats may still have cached tool descriptions; start a new
chat after refreshing.

For BYOK providers, ask the MCP host to show provider settings, or install `.[http]` and run
`companion settings`. This opens only a private local credentials form. Configure speech there
and set the character's `voice_id` through the SDK or character tool. **Apply to this process**
updates the process that opened it. **Save for future starts** additionally persists configuration
(macOS Keychain for keys, a private file on other platforms); other processes must restart.
Do not enter keys in a ChatGPT message. Audio is loaded on click through bounded, receipt-scoped MCP reads, not a
public localhost URL. Actual ChatGPT card rendering and app-only chunk reads were verified on
2026-11-05. The first playback attempt was blocked by the host's `media-src` policy for Blob
URLs. Player resource v2 explicitly declares `blob:` in both standard and compatibility CSP
metadata; the runtime was reloaded and host refreshed. The user subsequently confirmed playback
in ChatGPT. Resource v3 replaces the large player card with a tap-to-play voice bubble. Refresh
tools and render a fresh message to see upgraded resources; old messages may retain cached UI.

To package the native skill for the existing private registered app, run
`companion plugin-package ./output/companion-engine-plugin.zip --app-id <registered-app-id>`.
Upload/update that package through the host's supported plugin workflow; this is separate from
MCP tool discovery. Do not edit the host's installed plugin cache: it is not the source of truth.

The local runtime alias/profile is `companion-local`. Check it using
`tunnel-client runtimes status companion-local --json`, or stop it using
`tunnel-client runtimes stop companion-local`. It must remain running and the computer must
remain awake and online. This setup does not install automatic startup after reboot.
The runtime credential uses `env:CONTROL_PLANE_API_KEY`; no literal key is stored in the profile.
Restart requires supplying that environment variable securely. The initial credential was
configured for seven days and will need rotation; never commit credentials to this repository.

A public service needs OAuth/session identity, owner derived from verified identity, quotas,
authorization, deletion/export and monitoring. Local bearer tokens are not multi-user auth.
This private tunnel uses stdio, so it does not expose the loopback HTTP server or bypass its
Host/Origin/bearer guards. "No authentication" in the plugin means no additional MCP OAuth;
OpenAI tunnel identity and organization/workspace access controls still apply. HTTP integrations
must separately satisfy the server's Host/Origin/auth checks; do not bypass guards to get a public URL.
References: [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels),
[Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt).

## Provider references

- [ElevenLabs TTS](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
- [MiniMax speech](https://platform.minimax.io/docs/api-reference/speech-t2a-http)
- [Images](https://developers.openai.com/api/reference/resources/images/methods/generate)
- [Embeddings](https://developers.openai.com/api/reference/resources/embeddings/methods/create)
- [Transcriptions](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)

Request/response shapes have mocked HTTP tests, not live account validation. A provider saying
“compatible” does not guarantee all fields/models. Submitted data leaves the computer.
