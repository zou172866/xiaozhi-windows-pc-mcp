# Companion Studio

An optional, responsive web example for the Python SDK. The visual language follows
Anima's website: warm paper, ink, restrained coral, serif headlines and quiet motion.
This is a local developer application, not the hosted Anima product.

The core Python wheel contains **no chat frontend**. This directory owns its React,
TypeScript and Vite application. The Python launcher serves the compiled files and the
existing authenticated API on one loopback origin. No Convex or Anima account is required.

## Start

From the repository root, with Python 3.11+ and Node 22.12+:

```bash
source .venv/bin/activate
python -m pip install -e '.[http,calls]'
npm ci --prefix examples/showcase
npm run build --prefix examples/showcase
python examples/showcase/serve.py --open
```

Open the **private URL printed by the launcher**. It contains a random access token in
its fragment. The app removes the fragment after loading and keeps the token in that
browser tab's session storage. A new server process creates a new token.

Create a character, then open **Provider settings**. Configure your model's base URL,
model ID and API key. It must support Chat Completions with JSON-object responses.
Credentials remain on the Python server. Saving is optional: on macOS, persisted
secrets use Keychain by default; elsewhere, the engine uses a private local file.
The interface never reads back or puts provider keys in browser storage.

The launcher explicitly loads the engine's saved settings plus `COMPANION_*`
environment overrides, like `examples/chat.py`. See [provider configuration](../../docs/features.md).
**No model calls happen on startup or when saving configuration.** Your providers'
charges apply when you send a message, ask for a picture, transcribe a recording,
or generate speech.

### Preview without a model

```bash
python examples/showcase/serve.py --demo --open
```

This mode is conspicuously labeled **offline / scripted**. It creates fictional Rina
and Sean starter records in a separate preview database. Replies are scripts, but chat
persistence, review, retrieval, memory editing and source inspection use the actual
engine. It does not load your provider credentials. Do not use it to assess model quality.

## What works

| Interaction | Implementation |
| --- | --- |
| Create/edit/switch characters | Real `CharacterSpec` records, isolated per character. Starter personas are optional. |
| Chat | Real `Runtime.chat`, persisted history, previous messages, grounded memory retrieval. No fake token streaming. |
| Memory saving | Automatic, review or off. Pending memory proposals can be approved or the reply saved without them. |
| Memory drawer | Source quotes, manual correction with revision conflict checks, and deletion. Removing a memory retains its source conversation. |
| Reply context | Exact saved reader input and retrieval metadata; downloadable JSON. It is not hidden model reasoning. |
| Vision | Up to 4 PNG/JPEG/WebP attachments; 10 MB each, 20 MB combined. Raw image bytes are not retained as chat pictures; the engine stores observations and provenance. |
| Character reference | Create/edit character → Appearance: upload, preview, replace or remove a PNG/JPEG/WebP (up to 10 MB), plus optional appearance notes. Stored privately per character. |
| Pictures in chat | Ask for a selfie or character picture. The SDK uses the saved reference and displays the actual result in the conversation. Refreshing retains it. |
| Voice output | Explicit per-reply generation and playback. Requires a speech provider and a character voice ID. |
| Voice dictation | Click the microphone to record; click again to stop and automatically transcribe into the draft. Review/edit before sending. Cancel discards the audio without uploading. |
| Live calls | Doubao streaming ASR → the same memory/reply runtime → character voice; optional Vidu S2 animated video. |
| Export | Download the character's engine export, including conversation and memory data. |

Live voice and Vidu S2 animated video are available through the phone/video buttons
([setup](../../docs/live-calls.md)). Cloud sync, Live2D and public multi-user authentication
are not included. The composer image and microphone buttons use the configured vision
and transcription providers. The microphone requests
browser permission only when clicked, shows elapsed time, and releases the device when
stopped, cancelled or the page closes. Recordings stop at two minutes (20 MB maximum).
Chrome uses WebM/Opus when supported; MP4 and Ogg are alternatives selected by browser
capability. The app sends the actual recording container type to the existing API.

Configure an **Image generation** provider with generation and reference-edit support.
Add a character reference or appearance notes, then send e.g. “Send me a selfie by
the window.” No separate generation button is needed. Saving appearance alone makes
no provider call. The reference is sent to the configured image provider when an
image is requested; it is separate from the composer's image-analysis attachments.
Images are labeled as generated and can be downloaded. Failed image calls show an
error beside the reply and are not retried automatically. Replaying the same chat
request reuses its saved result. Ordinary chat does not request an image.

## Data and context

- Normal storage: `output/showcase/brain.sqlite3`; preview: `output/showcase/preview.sqlite3`.
  Use `--storage /your/path/brain.sqlite3` to choose another database.
- Media files live next to the database in the engine's `<database-stem>-media/` directory.
  Reference metadata and generated-image links live in SQLite; back up the database
  **and** this directory to preserve pictures. JSON conversation exports do not bundle
  image bytes. Removing a reference removes that reference file, not earlier generated pictures.
- One persistent conversation per character; there is no misleading “new chat” button
  that secretly keeps feeding the old conversation to the model.
- The launcher enables `allow_chat_images=True` (the headless SDK default is false).
  Other context defaults: 6 recent exchanges, 12,000 recent-context
  characters, 48,000 total-context characters, lexical memory retrieval and existing
  SDK caps. Captured input snapshots are enabled for inspection.
- Optional paid background embedding/summary jobs are off. `--adaptive-recall` opts
  into bounded model-assisted lookups during a reply; this can add model calls.
- Pending/failed operations are never automatically retried. A saved draft can be
  reviewed; an unfinished generation can be explicitly discarded. Check provider
  billing before repeating a failed paid request.

`capture_context=True` intentionally saves additional private context in the local
SQLite database. Treat the database, exports and `output/` as private artifacts.

## Development and verification

```bash
npm run build --prefix examples/showcase
npm run check --prefix examples/showcase
npm test --prefix examples/showcase
```

The browser check starts and stops its own offline server against a temporary database.
It exercises actual local chat, memory review, correction, deletion, source inspection,
export, provider-settings forms, microphone start/stop/cancel and permission failures,
mobile layouts, reduced motion and Axe accessibility checks. Microphone checks use a
synthetic browser audio device and mocked transcription responses, never your real mic.
It never calls a paid provider. Screenshots and a report go to
`output/showcase-checks/` (gitignored).

The check uses installed Chrome on macOS, or Playwright Chromium elsewhere. Install
Chromium if needed:

```bash
cd examples/showcase
npx playwright install chromium
```

Override `PYTHON` or `BROWSER_EXECUTABLE` for another interpreter/browser installation.
The Python API security tests live in `tests/test_showcase.py`. Provider-specific media
contracts are covered by the existing SDK and media tests; live vendor QA is separate.

Use `npm run format --prefix examples/showcase` before committing UI changes. Rebuild
and refresh the Python-served page to verify the real same-origin integration. Vite's
development command is for component styling only; no permissive CORS proxy is installed.

## Structure

```text
src/App.tsx          Conversation state, composer and media operations
src/components.tsx   Accessible drawers, character form and provider settings
src/memory.tsx       Memory editing and saved-context inspection
src/useVoiceRecorder.ts Browser recording lifecycle, formats, limits and cleanup
src/api.ts           Typed local HTTP boundary and tab-scoped authentication
src/style.css        Anima-aligned tokens, responsive layout and reduced motion
serve.py             Real SDK lifecycle + loopback HTTP launcher
demo.py              Explicit offline fixtures for preview/tests only
tests/smoke.mjs      Browser flows, screenshots and accessibility checks
```

Motion provides restrained transitions; Radix Dialog provides modal/focus behavior;
Lucide supplies icons. Fonts are served locally from the locked Fontsource packages.
Node dependencies, build output, SQLite files and run artifacts are gitignored and
excluded from release archives. This example is optional, never imported by the SDK.

Public Anima artwork in `public/portraits/` is copied from the sources documented in
[asset provenance](../../docs/assets/README.md). Those brand materials are separate from
the MIT-licensed code; replace them when branding your own product. Starter character
behavior is illustrative, not the production Anima character implementation.


## Voice, video and architecture

The phone/video buttons open a call invitation. Configure Doubao voice and ASR in Settings,
set the character’s voice ID, and add a Vidu key + appearance reference for animated video.
Your camera starts off. See [the complete call guide](../../docs/live-calls.md).

Open `/?view=architecture` (English) or `/?view=architecture&lang=zh-CN` (简体中文)
for the Motion-powered **Brain Atlas**. Switch languages in the header; four views show
semantic/episodic memory, retrieval and recency, consolidation, and emotion. Formula
sliders illustrate open-loop priority and PAD/tension decay. See [the exact mechanisms](../../docs/brain-flow.md).
It is a public static illustration with no access to your private conversation data.

The RTC SDK is loaded only when joining a video call (a separate roughly 2MB uncompressed
bundle). Voice-only use does not download it. Call/provider keys stay on the server;
the browser receives a short-lived RTC room token. This loopback example remains single-owner.
