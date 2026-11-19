# Verification

## Initial Anima Brain source publication (2026-11-13)

- Public repository: [`spearsheep/anima-brain`](https://github.com/spearsheep/anima-brain).
  The Python package/import, CLI and existing host identifiers remain unchanged.
- **193 Python tests passed**; Ruff and the synthetic offline memory demo passed.
- The optional showcase production build and offline browser checks passed, including
  memory/chat flows, voice and call controls, the architecture walkthrough, four viewport
  sizes, reduced motion and accessibility. The build reports a large RTC-related chunk;
  frontend bundle size remains an optimization opportunity.
- Built wheel and source archive successfully. A fresh virtual environment outside the
  checkout imported the base wheel without HTTP/MCP dependencies; installing the optional
  HTTP/call extras then passed **29 SDK tests** and `pip check`.
- Publication inputs are source, tests, example configuration, locked frontend dependencies,
  prebuilt MCP cards, MIT/third-party notices and documentation assets. Private databases,
  credentials, generated host bundles, media, build outputs and dependency directories
  remain ignored. File-pattern scans found no credential matches; this is not an external
  security audit. Private vulnerability reporting is enabled on GitHub.
- These are local validation results. GitHub Actions provides a separate Linux/Python
  matrix check; consult the repository's Actions tab for the result of a particular commit.

## Interactive Brain Atlas (2026-11-13)

- Four views cover semantic/episodic memory, RRF versus time-aware open-loop ranking,
  cited consolidation, and character emotion. Numeric examples were checked against
  the current Python formulas; no memory or emotion behavior was changed.
- Production frontend build and the full offline browser suite pass. Atlas checks cover
  four views at 390, 768, 1024 and 1440 pixels, with no page overflow or clipped node labels.
  Axe reports zero violations in those 16 reduced-motion view/viewport combinations.
- Keyboard-operated sliders and optional-channel/date toggles reproduce the documented
  RRF, follow-up priority and PAD/tension decay examples. Play, pause, restart and step
  navigation work; reduced motion disables playback. No private API requests were made.
- README SVG and GIF share versioned diagram data with the interactive page.
  Screenshots are local under `output/showcase-checks/` and `output/brain-animation-v2/`.


## Live calls and visual architecture (2026-11-12)

- Real Doubao Seed ASR accepted a streamed Chinese utterance. The same memory/reply
  runtime generated a response; the configured Rina voice streamed **190,298 bytes**
  of PCM24k output. That probe took approximately **3.7 seconds from final ASR to
  validated reply text**, before playback. This is one local probe, not a latency benchmark.
- A real Vidu S2 Component session returned managed Volcengine RTC credentials. Chrome
  decoded **1024×1024** video; the frame counter grew from **4 to 318** across the reply.
  No browser JavaScript errors were observed. Both paid test sessions were independently
  queried through Vidu’s status API and reported **ended** after hangup.
- Provider testing used a synthetic spoken input and isolated test database. No physical
  microphone or camera was accessed. It does not establish echo performance in a real room,
  mobile Safari support, or voice/avatar quality for every character and language.
- Automated call tests cover framing, bounded ASR payloads, speech completion, actual
  memory commits, review policy, per-character concurrency, interruptions, provider cleanup,
  setup cancellation, owner-scoped recovery and WebSocket origin/token checks.
- **193 Python tests** and the full offline browser suite pass. Browser checks cover four
  chat viewports, mobile call controls, audio capture, permission rejection, mute/interrupt,
  media-track release, keyboard access and reduced motion. Axe found zero violations
  in the tested call and architecture views.
- The standalone Motion walkthrough has a code-generated static SVG and a compact GIF
  for GitHub. It documents the implemented flow and uses illustrative text only.
- Private logs, audio and screenshots are in `output/live-calls-20261112/` (gitignored).
  Public setup and limitations are in [live-calls.md](live-calls.md).


## Chat pictures and appearance references (2026-11-12)

- 183 Python tests passed. Added coverage of actual reference bytes in multipart
  image edits, description-only generation, capability gating, missing appearance,
  memory review, restart/replay without extra provider calls, owner-scoped access,
  invalid uploads, rollback on disk failure, replacement/removal and pending-turn guards.
- Live configured providers: a normal chat request generated a 1024×1024 character
  selfie from the saved Anima Rina portrait. Ordinary conversation and a quoted,
  explicitly negated image request produced text only. The generated image was inspected.
- The requested local junior voice clone returned valid MP3 audio (24,429 bytes).
  Voice IDs and credentials remain in local character/settings storage, not source defaults.
- TypeScript/Prettier, Ruff and the production build passed. The offline browser suite
  passed with real reference upload, reload, removal and authenticated image display,
  including the mobile appearance form with no Axe violations. The existing four
  viewport, memory, settings and microphone checks also passed.
- The actual local Rina chat then generated another selfie through HTTP. Chrome
  displayed it after a reload and confirmed the saved reference and junior voice ID.
  Existing character history was retained; a SQLite backup preceded local configuration.
- Private live artifacts: `output/chat-images-20261112/` and
  `output/local-setup-20261112/`. These are gitignored. Tests below that predate this
  change describe historical behavior; image generation is now available through chat.

## Optional Companion Studio (2026-11-11)

The new frontend is a **separate source example** at `examples/showcase/`, not a
bundled SDK UI. Its style follows Anima's website; no production Anima code, Convex
functions or mobile apps were modified.

- **176 Python tests passed**, including new public-assets/private-API boundaries,
  owner-scoped saved-context access, pending memory review, replay without regeneration,
  correction revisions, source-preserving deletion, and image-input validation.
- TypeScript checks, Vite production build, Prettier, Ruff and npm dependency audit
  passed. The audit reported no known vulnerabilities at the time of verification.
- The repeatable Playwright test uses a temporary database and a clearly labeled
  scripted provider. Actual local HTTP, storage, chat/review, correction/deletion,
  context inspection, export and provider-configuration forms were exercised.
- Chrome layouts were inspected at **375, 768, 1024 and 1440 pixels**, including
  reduced motion. No horizontal page overflow; Axe reported zero violations of its
  enabled WCAG A/AA checks across the tested chat, welcome, memory, inspector,
  character, pending-review and settings views. Automated checks are not a complete
  accessibility certification or a physical iOS/Android device test.
- Screenshots and machine-readable checks are in `output/showcase-checks/` locally.
  Run `npm test --prefix examples/showcase` to reproduce them with no provider spend.
- Wheel remains headless; source archives include the optional frontend source and
  lockfile, excluding Node dependencies, compiled frontend output, databases and run
  artifacts. Release private-file/pattern checks pass.

The composer now has image attachment and microphone dictation controls. The image
generation button was removed; image generation remains available in the SDK/API.
Recording opens the microphone only on request, displays elapsed time, releases it
on stop/cancel/navigation, and automatically transcribes the stopped recording into
an editable draft. Dictation does not automatically send a chat message.

The dictation browser checks passed with Chrome's synthetic microphone and mocked
transcription responses: real WebM and MP4 encoding, draft preservation, cancellation,
permission denial, late permission after cancellation, provider failure without retry,
empty transcription, the two-minute limit, and microphone release on page exit.
No physical microphone was accessed. TypeScript/format/build checks and the 21
targeted HTTP/media/showcase tests passed again after this change.

No new live paid model or media calls were made for this frontend change. Vision,
transcription and speech controls use the existing API; provider-specific
contracts retain the SDK's mocked tests. Real vendor connectivity, voice choice and
model quality depend on the user's configuration. Realtime calls/avatar rendering
are not implemented by this example.

## Developer-only interface 0.6.0 (2026-11-11)

The old bundled chat frontend has been removed. Use the SDK, terminal example, MCP,
or `companion http` for the loopback API. `companion settings` opens only a private
provider-configuration form. MCP memory, voice and settings cards remain available.
Earlier sections below are historical verification records; their `web`/`ui` extras,
chat frontend commands and screenshots no longer describe the current release.

168 tests pass after removal, including settings-only route isolation, redacted
configuration responses, MCP settings launch, preserved API flows and absent old
chat assets/CLI flags. No new paid provider requests are needed for this change.

Local Chrome checks verified apply, save and clear with temporary synthetic credentials,
redacted reads, token removal from the address bar, and rejection of chat routes. README
previews were inspected at desktop/mobile widths and in dark mode; all images loaded and
there was no page overflow. This was a local Markdown preview, not a published GitHub page.

Fresh wheel installation outside the checkout passed SDK import and the offline memory
example without HTTP or MCP dependencies. Adding the `http` extra passed HTTP/MCP imports
and `pip check`. Wheel/source archives exclude the removed chat assets; MCP cards and the
settings form remain, while Anima imagery is documentation-only. Local documentation links,
Ruff and release private-file/pattern checks pass. No publish or host reconfiguration was done.

## Configurable multimodal SDK 0.6.0 (2026-11-11)

166 tests pass, Ruff passes, and `pip check` passes. The 15 added tests cover image
bytes through perception, source-linked memory commit and review; changed-image
replay conflicts; no text fallback on vision failure; cross-instance pending-turn
protection before vision spending; saved-draft recovery; memory read-disable;
separate write policy and recent-history budget; follow-up admission into full
retrieval lanes; independent summary provider/worker availability; image options,
reference edits, multiple artifacts, ownership checks; speech/transcription options
and absence of implicit conversation writes. One upstream Starlette/AnyIO
deprecation warning remains.

An actual `parcc/GLM` smoke used two synthetic messages with no lookup planner and
recent history disabled. It saved the jasmine-tea fact, recalled it after restart,
and replayed the completed request with credentials removed. All checks passed.
Reported totals: 8,539 input tokens and 1,166 output tokens; about 6.31 and 3.08
seconds per call. Dollar cost is unknown. The private trace is
`output/sdk-multimodal-20261111/live-text-smoke.json` and is excluded from release.

Vision, image edits/generation, speech and transcription were tested with mocked
provider responses and real request serialization, local storage and replay. No
claim of real-provider visual fidelity, speech quality or universal model support
is made. Input fixtures are synthetic protocol fixtures, not quality evaluations.

The 0.6.0 wheel and source archive build successfully, each under 1 MiB. A fresh
environment installed the wheel with `[ui]` and pytest, without MCP. From outside
the source checkout, 29 SDK tests passed against the installed wheel; static UI
assets and public exports were present. Dependency consistency and release
private-file/pattern checks passed. CI now includes that installed-wheel SDK test.
No remote CI run, release publication, push or installed-host reconfiguration was
performed. Raw private databases, generated media and output traces remain ignored.

Start with [SDK capabilities and limits](sdk-capabilities.md) or
`python examples/multimodal.py --help`.

## CompanionBrain SDK 0.5.0 (2026-11-11)

151 tests pass. Ruff passes. Tests cover the async and blocking API, fresh-process
persistence, owner isolation, exact replay, review/decline, concurrent duplicate
requests, async-waiter cancellation, commit-failure draft recovery, configuration
preflight, atomic character/policy creation, and shared SDK/local-UI defaults.
One upstream Starlette/AnyIO deprecation warning remains; it does not fail tests.

Real provider smoke used the already configured school `parcc/GLM` endpoint with
synthetic messages only. Two calls, no extra lookup planner, recent history limit 0:

1. "Please remember: my favorite tea is jasmine." saved one grounded fact.
2. After closing/reopening the database, "What is my favorite tea?" returned jasmine.
   Stored memory/original evidence was delivered despite the disabled recent window.
3. Reopening without model settings and repeating the same request ID returned the
   committed answer without a provider call.

Reported usage: 8,537 input tokens and 1,130 output tokens total. Calls took about
6.66 and 2.56 seconds. Dollar cost is unknown; no provider price was configured.
The private local trace is `output/brain-sdk-20261111/live-smoke.json` and is excluded
from release artifacts. This proves a small end-to-end memory flow, not emotional
quality, multi-provider compatibility or production load capacity.

Built both a wheel and source archive, with explicit package inclusion. The wheel
is approximately 563 KiB, including static UI resources and license notices. A new
virtual environment installed the wheel with `[ui]`; from outside the source checkout,
the async SDK, persisted reply, local UI assets and authenticated chat API passed.
No MCP package was installed for that minimal UI test. `pip check` passed with newly
resolved dependencies. A separate full `[web]` environment also installed successfully.
The minimal `ui` extra avoids requiring MCP and its crypto dependencies for ordinary chat.

The offline example, documentation links and source/archive private-file pattern
checks passed. MIT license, contributor/security docs and GitHub CI configuration
are included. CI has not run on a remote repository, and no public repository,
package upload or installed-host reconfiguration was performed in this change.

The implementation references Anima's bounded reader/final-reply separation and
conversational response-depth guidance. It does not import that application's
characters, credentials, backend, customer data or deployment configuration.

## Claude / DeepSeek Harness local integration (2026-11-06)

137 Python tests pass; Ruff passes. New tests launch the exported stdio commands
in separate processes and verify shared persistence, corrected current facts,
idempotent commit replay, character/owner isolation, configuration-only export,
secret exclusion and refusal to overwrite an existing package.

Claude Code 2.1.42 validates the exported plugin (one optional author warning).
DeepSeek Harness 0.2.2-rc.1 on isolated Node 24.11.1 composes the exported overlay.
Its actual Cordis MCP bridge registers 48 Engine tools and passes write,
fresh-session recall and idempotent replay using disposable data. The maintained
`examples/deepseek_bridge_smoke.mjs` reproduces that check without model inference.

Local exports are under `output/host-integrations-20261106/`; the isolated Harness
runtime is under `output/host-runtime/`. Both are ignored. The new exported database
and provider settings paths are separate from the existing private ChatGPT runtime.
No host configuration was overwritten and no paid inference calls were made.
Claude/DeepSeek model-driven tool-use compliance and their media UIs remain unverified.
See [host integrations](host-integrations.md) for setup and exact verification scope.

## Brain adaptation 0.4.0 (2026-11-05)

135 tests pass; Ruff lint and formatting checks pass (48 files); `pip check` passes.
Fourteen new tests cover layered retrieval beyond recent windows, vector invalidation,
state-key conflict candidates, temporal uncertainty/pagination, timezone-aware dates,
idempotent historical analysis repair, grounded response-style invalidation, selective
episode invalidation, separate character self-memory, media observation provenance,
restart/owner isolation, explicit automation-off policies, bounded host summaries, and
an actual stdio MCP client exercising the new tools. These are functional tests, not
evidence of improved emotional quality or host instruction-following.

A read-only backup of the owner's database was upgraded in a disposable directory.
Hashes of every row in seven original tables (characters, turns, memories, revisions,
evidence, documents, turn_appraisals) remained unchanged. The copy gained 13 derived
retrieval entries; the live database was not migrated by this check.

The isolated wheel build succeeded; inspection confirms the new modules and bundled
skill are included. The 0.4.0 native plugin ZIP was exported and checked for accidental
database/environment/private-key files. No paid provider calls were made.

The private tunnel and MCP process were subsequently restarted on November 5 using
the existing process environment in memory, without persisting or displaying the runtime
key. `/readyz` reports ready, and the connected `list_characters` call returns the new
timezone field. A pre-reload SQLite backup was retained with mode 0600. All seven original
tables still match that backup after migration; 13 derived index entries were added.
Persona and configured voice are unchanged. Host tool rediscovery is still unconfirmed:
Chrome's native screenshot returns a solid blue frame, and the attempted Refresh tools
action produced no observed discovery. New-tool discovery itself was verified through
a fresh local stdio process. No production makima.io deployment is part of this release.

## Private owner credential tool (2026-11-05)

116 tests pass; Ruff passes. The optional configure_private_provider tool is absent by default
and enabled only by COMPANION_ALLOW_CHAT_CREDENTIALS=1. Tests cover immediate application and
persistence, blank-key preservation, malformed endpoints/fields/services, sanitized errors,
failed-save preservation, and unchanged character DB contents. No real provider credential was
changed and no paid API request was made. Settings patches share validation/persistence with the
local web UI. The user's private tunnel was restarted with this explicit opt-in and reports
ready/healthy. Keys submitted this way necessarily remain visible to the host chat/tool transport;
local redaction does not erase upstream history or logs. Public plugin workflow stays local-only.
A separate real stdio client discovered the tool and saved a synthetic key in a disposable
configuration with a redacted response. ChatGPT Refresh tools was requested, but its UI remained
disabled/loading; actual discovery in a new ChatGPT conversation is not yet confirmed.

## Host-native cards, private settings, flexible delivery (2026-11-05)

108 tests pass; Ruff checks and UI build pass. New tests cover atomic Keychain migration,
clearing secrets, save failure preservation, credential redaction, app-only settings launcher,
real loopback HTTP authentication/origin rejection, shared-runtime settings updates, and identical
persona/guidance supplied to host and standalone reply provider. No paid provider calls.

Voice, memory and settings cards share neutral host CSS variables, system fonts, light/dark
fallbacks and focus targets. Inline memory is a compact preview with two actions; source browsing,
editing and section navigation live in the host-requested expanded view. Local Chrome bridge
verified inline and expanded memory rendering. Remaining card visual checks and actual ChatGPT
tool refresh were interrupted by ScreenCaptureKit error -3811 and concurrent browser use; do
not treat the local bridge as verification of the new cards in every ChatGPT client.

Existing provider settings were migrated to macOS Keychain and round-trip compared without
printing credentials; the mode-0600 configuration file has no key fields. The private tunnel
was restarted, reports ready/healthy, and the connected MCP list_characters call succeeded.
ChatGPT must refresh its tool discovery to expose the new settings card. Model compliance with
shared delivery guidance is not guaranteed by these deterministic tests.

## Host-authored summaries (2026-11-05)

104 tests pass. Four added tests cover provider-free host summary batches, bounded source
windows, original citation validation, stale fact rejection, tenant scope, disabled memory,
mutually exclusive writers, durable idempotent replay, MCP calls and preserving embedding
settings when switching. Ruff and the rebuilt UI bundle pass; the updated packaged Skill
passes skill-creator validation. Luna is configured for host summaries; paid background
summaries, embedding and semantic retrieval remain off. No provider key was added.
The private runtime restarted successfully. Final ChatGPT tool refresh and live host-summary
execution remain unverified: Chrome stopped exposing page content and then reported user
interaction during the refresh attempt. No real memory summaries were written in this check.

## Automatic maintenance and memory panel (2026-11-05)

100 tests pass; Ruff checks and UI bundle builds pass. New tests cover durable restart backlog, per-character concurrent claims, provider failure
quarantine, lease expiry, explicit retry, daily job caps, disabled/missing providers, summary
threshold/idle flush, reindex after contextual notes, cached per-turn query embeddings,
lexical fallback, concurrent-fact summary rejection, HTTP configuration, background processing
and revision-checked MCP panel edits/deletion. These use isolated fixtures and mock providers.

Local Chrome bridge verification: panel rendering, editing a synthetic fact persisted
Revision 2, Chinese controls and dark mode at 280px width. A partial host-context update
initially reset the theme; this was fixed and visually rechecked. The private runtime was restarted and ChatGPT tool discovery refreshed. In the actual
[ChatGPT test conversation](https://chatgpt.com/c/6aecf947-5fbc-83ea-8439-bbd18c0d2a99),
`show_memory_panel` rendered seven existing Luna facts with source/edit/delete controls.
The Processing tab reported 22 excerpts, 11 pending turns and automation switches off.
The host displayed its pre-existing “CSP off” developer setting; it was not changed, so this
is not verification under enforced host CSP. No paid provider calls or real user-memory
edits were made during these checks.

## Compact voice bubble (2026-11-05)

Player v3 replaces the large card and native controls with an incoming message bubble, a
character initial, real duration, animated speech indicator, and an optional save button.
User-initiated loading and playback share one tap; another tap pauses. English/Chinese labels,
host theme changes, visible keyboard focus and reduced-motion styles are included. Resource
metadata requests no host border; the host ultimately controls its surrounding chrome.

Local Safari MCP-bridge verification: first-tap playback, simulated read failure and successful
retry, light desktop appearance, dark Chinese appearance at a 280px iframe width. Audio reads
increase on loading/retry only, with zero generation calls. Six plugin tests and Ruff passed.
The bundle was rebuilt, the private runtime reloaded and ChatGPT tool discovery refreshed.
In an actual [ChatGPT test conversation](https://chatgpt.com/c/6aecee89-7d8c-83ea-91f3-aa17c17291c0),
`show_voice_message` rendered the v3 bubble for the existing Luna MP3. The first tap loaded
the audio and started playback (Chrome reported audio playing); the second tap paused it.
The bubble displayed the real duration rounded to 14 seconds. No new synthesis was requested.

## Actual ChatGPT player integration (2026-11-05)

Refreshed the existing private plugin through ChatGPT settings. Its tool dialog showed 29
tools. In a new ChatGPT conversation, `show_voice_message` rendered the embedded player and
its click-to-load `read_voice_chunk` call delivered the existing 109,101-byte Luna MP3.
The local file decodes as 13.632 seconds. No new synthesis was requested: the operation
journal remained at four entries throughout this playback test.

Actual playback initially failed. Chrome's console identified a `media-src` CSP violation
for the component's in-memory Blob URL. Resource v2 now declares `blob:` in both `_meta.ui.csp`
and `openai/widgetCSP`. Real local stdio inspection confirms that metadata; the private runtime
was restarted with its existing credential and a second host tool refresh was requested.
Six plugin-support tests and Ruff check/format pass; the wheel was rebuilt. These checks do
not establish that ChatGPT accepts the revised CSP. Final in-host playback remains unverified:
Chrome stopped exposing page content during the follow-up test. The user subsequently confirmed
that loading and playback work in ChatGPT. No security settings were disabled.

## Private tunnel and Doubao clone verification (2026-11-05)

87 tests pass; Ruff check and format checks pass. New coverage exercises the
Doubao clone 2.0 request/stream contract, terminal completion, malformed/error
streams, size limits, receipt replay and compressed HTTP responses. The shared
HTTP helper now avoids decompressing an already decoded response a second time.

The existing private tunnel was restored and reloaded without creating a new
credential or changing its scope. Actual connected-plugin `list_characters` and
`generate_voice_message` calls succeeded. Luna uses the configured warm junior
clone. The short generated MP3 is 19,437 bytes, decodes as 2.424 seconds, and its
provider receipt reports six text words. Provider failures before the compression
fix may still have incurred charges; they were not automatically retried.

After runtime reload, one host request timed out before reaching the local
operation journal. After a successful read initialized the connection, replaying
that same request ID succeeded. No second operation was created for that ID.

Provider credentials are configured in the private local settings file (0600,
excluded from Git). The tunnel credential is held by the running process, not
written to source/config. These checks use the user's authorized provider accounts;
they are separate from the offline tests described below. Actual ChatGPT embedded
playback status and refresh of the four newer tools are documented in the newer entry above.
The computer and tunnel process must remain available for the private connection.

## 0.2.1 companion workflow and player (2026-11-03)

70 tests passed; Ruff check/format, skill-creator validation and pip check passed.
New coverage includes real stdio discovery/instructions/workflow, MCP Apps resource metadata,
UI-only chunk results, byte-exact multi-chunk reconstruction, receipt owner/character isolation,
path traversal/symlink/missing file/image rejection, private optional provider persistence,
environment precedence, secret redaction, plugin ZIP allowlisting, and a multi-turn
remember → restart → supersede → pending recovery → idempotent commit flow.
These are deterministic functional tests, not an LLM's skill-following evaluation.

Manual Chrome verification used `examples/player_preview.py`: real MP3 decoding/playback,
no autoplay, renderer reset, synthetic read failure and retry, with zero provider-generation
calls. The host is a local MCP Apps bridge simulator, not ChatGPT. Screenshot:
`output/voice-player-qa.png`. The real local UI was separately tested against a temporary DB:
provider/model/hidden test-key entry, opt-in save, restart notice, no key re-display, and the
save checkbox resetting to off. Screenshot: `output/provider-settings-qa.png`.
No real API key, paid provider, production data or account configuration was used in these tests.

`ui/package-lock.json` pins the component dependencies. `npm ci && npm run build` produces the
bundled player and dependency license notices; ordinary Python users do not need Node.
Wheel inspection confirms the skill, reference, manifest and player resource ship together.
The exported native plugin ZIP contains authored skills and an optional registered-app mapping,
not the Python engine, DB, provider settings, media or tunnel credentials.

Outstanding at this release: refresh host tool discovery, install/update the native skill bundle
if desired, then verify actual ChatGPT iframe/CSP/app-only calls. See the newer entry for actual
host testing and its remaining playback limitation. Tunnel restoration, real provider
configuration and connected-plugin speech generation are verified in the newer entry above.
Do not mistake the original 25-tool private connection for the updated 29-tool release.

## Original 0.2 baseline

Development verification: 2026-10-29. **64 tests passed**, Ruff check/format passed,
bytecode compilation and `pip check` passed. The wheel
`dist/companion_engine-0.2.0-py3-none-any.whl` builds with all three bundled UI assets.
Desktop (1440×1000) and mobile (390×844) Chromium smoke passed with zero browser errors
and zero paid API calls. One dependency deprecation warning comes from Starlette's test client.

```bash
.venv/bin/ruff check src tests examples
.venv/bin/ruff format --check src tests examples
.venv/bin/pytest -q
.venv/bin/python examples/offline_demo.py
.venv/bin/pip check
.venv/bin/python examples/ui_smoke.py
```

Coverage includes persistent memory/restart; owner/character isolation; quotes and revisions;
atomic rollback; retries/concurrency/pending recovery; English/CJK search; original attribution;
notes/links; semantic vectors and stale-index invalidation; historical supersession; core pinning;
maintenance completion/resume; v1 history migration; history pagination; delete cascades; exports;
production-reference emotional dynamics and decay; review/automatic/off modes; bounded reader;
provider usage; HTTP contracts for ElevenLabs/MiniMax/images/transcription/embeddings; operation
replay; bearer/Host/Origin guards; redacted settings; actual stdio and HTTP MCP initialization/tools.

Browser smoke uses Chromium, a synthetic provider and temporary DB. It creates a character,
chats, approves a memory, reloads persistent history, checks provider settings, exercises mobile
layout and records screenshots in `output/ui-smoke/`. No paid API call is made. Screenshots are
UI evidence, not real model output or emotional-quality evidence.

No real API keys, paid inference, actual ChatGPT/Claude/Codex account connection, OAuth/tunnel,
production deployment, research benchmark or large-scale load test is included. Mocked contracts
do not establish provider quality/availability. Public release needs further integration/security
and license/provenance work. Dependency versions are in `constraints-tested.txt`; browser QA
additionally uses Playwright 1.64.1 (optional, not a runtime dependency).
