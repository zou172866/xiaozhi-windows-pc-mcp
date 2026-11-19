# Local providers and connection limits

Ask to show provider settings in the MCP host; its button opens a private local
credential form connected to that running engine. Alternatively, use
`companion --db /absolute/path/brain.sqlite3 settings` with the `http` extra installed.
This form configures providers only; it is not a chat frontend. Environment variables
also work. Do not ask the user to paste a key into chat or share a launch token.

- ChatGPT/Claude host inference: no extra reply-model key here.
- Voice messages: provider `elevenlabs`, `minimax` or `doubao`, provider model ID and API
  key. Set the chosen character's `voice_id` separately with the character tool or SDK.
  Doubao clone 2.0 uses the speech API key (not a MiniMax/ElevenLabs key), an
  owned `S_*` speaker, and model `seed-tts-2.0-expressive` or the supported
  standard model. The adapter requires the terminal success record before
  accepting the MP3. No clone is trained, retrained or substituted automatically.
- Semantic memory: compatible endpoint, embedding model and key. Indexing and semantic lookup default on unless explicitly disabled.
  A running worker indexes committed originals automatically; configured API use can incur fees. Local lexical memory needs no embedding key or database key.
- SDK reply generation, images and transcription each have separate settings.
  Do not assume a ChatGPT subscription exposes those APIs to the backend.

**Apply to this process** updates only the engine that opened the form.
**Save for future starts** explicitly persists configuration outside the repository, normally
`~/.config/companion-engine/providers.json`. On macOS secrets are stored in the OS
Keychain, with nonsecret configuration and key references in that file. Other
platforms use a mode-0600 local file. OS-account access and backups remain relevant. `COMPANION_SETTINGS_FILE` selects a different path;
provider `COMPANION_*` environment values take precedence on restart.
Other independently running processes must restart to load saved configuration. Do not
automatically change tunnel credentials, connection permissions or runtime state.
`config.example.env` is documentation, not an automatically loaded dotenv file.

`provider_status` returns readiness/booleans, never keys. The voice playback
card reads receipt-owned audio through bounded MCP tool calls; it needs neither
an internet-visible local port nor public object storage. Only the authorized
host/player receives the audio. No autoplay; playback does not call a provider.

The installed private MCP connection and a packaged native skill are separate.
If the native skill has not been installed, `get_companion_workflow` provides
the same instructions through MCP after the server is restarted/refreshed.
Do not claim all ChatGPT conversations are automatically captured, that tools
are forced every turn, or that another device can connect while this computer
is off. Public multi-user hosting/OAuth and cloud memory sync are not included.
