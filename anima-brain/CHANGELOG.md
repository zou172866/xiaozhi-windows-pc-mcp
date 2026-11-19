# Changelog

## Unreleased — memory architecture alignment

- Restore causal contextual raw/note embeddings, chunk offsets, typed linked old-note evolution,
  production-style archive ranking and explicit temporal reader actions.
- Add durable cross-key knowledge reconciliation, reinforcement/lifecycle metadata and current
  versus corrected/superseded filtering; regenerate affected core summaries.
- Add background structured notebook/episode metadata and separate character core support.
- New SDK characters enable configured memory maintenance by default, with 10 recent exchanges
  and a retained working window. Existing explicit off policies remain off; provider calls can
  increase. MCP host authoring remains available without a backend LLM key.
- See `docs/memory-parity.md` for behavior, migration and local infrastructure differences.

## Unreleased — live calls and architecture walkthrough

- Public project name: Anima Brain (`spearsheep/anima-brain`). The Python distribution,
  import path, CLI, persisted data locations and host identifiers retain their existing names.
- Optional streaming call SDK: Doubao ASR, the existing memory/reply runtime, character TTS,
  and Vidu S2 Component video over managed Volcengine RTC.
- Showcase phone/video controls, microphone permission, mute, explicit interrupt, optional
  camera snapshots, real connection progress, and cleanup on exit.
- Persistent call delivery receipts, concurrency protection and explicit crash recovery.
- Detailed brain-flow documentation, shared diagram data, static SVG, compact GIF and
  Motion-powered interactive walkthrough.


## 0.6.0 (unreleased)

- Added an optional, separately built Anima-aligned React web showcase: chat, character
  settings, memory review/editing, saved context inspection, image attachments and
  microphone dictation into the editable composer. Recording supports stop, cancel,
  elapsed time and automatic transcription.
  The core package and default HTTP command remain headless.
- Chat image requests now use the SDK's generation/reference-edit adapter, with
  persisted per-character appearance uploads, in-conversation images, downloads and
  explicit failure states. Studio enables this; SDK apps opt in with `allow_chat_images`.
  Reference changes are atomic and request replay does not regenerate images.

- Developer-focused README with public Anima branding, product links and an explicit
  description of this Python adaptation's capability boundaries.
- Removed the standalone chat frontend and its demo assets. SDK, CLI, HTTP API and
  MCP remain; memory, voice and settings cards still work inside supported MCP hosts.
- Migration: `companion web --open` becomes `companion http` for API clients;
  `examples/chat.py` is terminal-only. Install `.[http]` instead of `.[web]` or `.[ui]`.
  The internal app factory moved from `web.py` to `http_server.py`.
- Separate `companion settings` command and minimal private provider-settings form;
  the MCP settings card opens the same form against its own running engine.
- Typed image/audio inputs and async + blocking SDK image generation, reference-image
  edits, speech and transcription. Explicit independent vision and summary providers.
- Vision converts actual image bytes to source-linked observations; reader generation
  preserves the prepared context and validates visual memory provenance.
- Separate memory-read switch, retrieval lane limits and memory/recent-history budgets,
  reapplied after follow-up lookups. Character memory policy continues to control writes.
- Media options, multi-artifact results, scoped artifact access, request replay checks
  including attachments/options, and failures with inspectable request IDs.
- Configurable character appearance, voice and core persona at creation; no automatic
  image creation. Multimodal example and capability/configuration guide.

## 0.5.0 (unreleased)

- `CompanionBrain`: async `chat`, blocking `chat_sync`, typed replies, review,
  recovery, lifecycle and user-scoped persistent storage over the existing Runtime.
- Configurable context/history, application reply instructions, reader steps,
  provider timeout, temperature and output ceiling. Optional exact context capture.
- Successful replays preserve diagnostics and need no configured provider. Cached
  generation can be committed again after a save failure without another model call.
  The chat reservation now encloses retrieval as well as reply generation.
- Real-model terminal/local-UI example, MIT license, explicit package inclusion,
  private-file checks, contributor/security docs and CI configuration.
- Minimal `ui` extra for local chat without MCP dependencies; existing `web` extra
  retains HTTP MCP support. SDK and its UI share new-character automation defaults.

## 0.4.0

Local memory/emotion runtime, optional MCP and web UI, cited memory corrections,
layered retrieval, media, maintenance, and Claude/DeepSeek host bundle exporters.
