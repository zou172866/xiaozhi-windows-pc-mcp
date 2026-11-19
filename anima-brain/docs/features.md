# Feature reference

A local-first Python engine for persistent character memory, grounded response guidance,
fictional relationship dynamics, optional images, and streaming voice/video calls.

**For developers:** call the Python SDK from your application, connect an existing agent
through MCP, or use the authenticated local HTTP API. The Python package has no bundled chat frontend. A separately built
[optional web showcase](../examples/showcase/README.md) connects to the same local API.
No Convex, Anima account or hosted database is required. Public Anima imagery identifies the project and supplies optional showcase portraits. This is an implemented **local development release (0.6.0)**,
not a claim of production readiness or complete Anima parity.

## Start developing

Python 3.11+ with SQLite FTS5:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -c constraints-tested.txt -e '.[dev,http]'
.venv/bin/python examples/offline_demo.py
```

Use [the SDK](sdk.md) in your own application, or configure a Chat Completions-compatible
endpoint/model and run `python examples/chat.py` for terminal chat. See
[configuration options](sdk-capabilities.md). No provider is selected automatically.

- **Chat:** SDK orchestration with persisted turns, evidence retrieval and configurable reply instructions.
- **Memory:** automatic grounded fact extraction by default; review-before-saving and history-only remain available.
- **Relationship:** fictional mood/relationship continuity on by default for new characters; romantic tone remains separately opt-in.
- **Live calls:** optional streaming SDK and showcase controls; [setup and lifecycle](live-calls.md).
- **Voice:** explicit transcription and speech generation through ElevenLabs, MiniMax or
  Doubao clone 2.0. Your app handles recording, review and playback. Configure the provider,
  model, key and character's voice ID.
- **Images:** explicitly generate and save an image through a compatible Images API.
- **Tools:** correct/delete facts, organize notes/core, index excerpts, export, recover turns.

### Provider configuration

Use environment variables or `Settings(...)` in code. For a local credential form, run
`companion settings` after installing `.[http]`. It serves only provider settings, not chat.
**Apply to this process** is temporary; **Save for future starts** is explicit opt-in.
On macOS, API keys are saved in **macOS Keychain**; non-secret settings and a vault reference
remain in `~/.config/companion-engine/providers.json` (mode 0600). Existing file-based keys
migrate on the next explicit save. Other platforms use an unencrypted private file;
`COMPANION_SECRET_STORE=file` explicitly selects that fallback. No credentials are sent to ChatGPT.

In ChatGPT, ask to **show provider settings**. The neutral status card opens a private settings
page on the computer running Engine—even when you click from a phone. It never accepts keys
inside ChatGPT. That page updates the same running MCP immediately; separately launched
processes need restarting. Environment values override saved settings on restart;
`COMPANION_SETTINGS_FILE` selects the configuration path. No dotenv file is automatically loaded.
See [config.example.env](../config.example.env). Never commit keys.

### Optional private credential tool

For an owner-controlled private MCP connection only, launch the server with
`COMPANION_ALLOW_CHAT_CREDENTIALS=1` to expose `configure_private_provider`.
The host can pass an explicitly supplied key and provider configuration directly to the engine.
Changes persist to the configured secret store and apply to that running engine immediately.
The key necessarily travels through the host conversation/tool arguments; it is not hidden from
ChatGPT or removed from chat history. The engine does not journal this operation into character
history/memory, and returns status only. It makes no provider requests or paid-job changes.

This mode is **off by default**, separate from the public plugin workflow and not a credential
collection flow for a published ChatGPT plugin. The normal private local entry route remains
available. Independently running engine processes still need a restart to read saved settings.

## Use an existing agent

For **Claude Code, Claude Desktop or DeepSeek Harness**, see
[host setup and verification](host-integrations.md). Generate their local
configuration together with `companion --db /absolute/path/brain.sqlite3 host-package ./output/hosts`.
Claude and Harness reuse this same engine and companion workflow; no AstrBot is needed.

This path needs **no extra chat-model API key**: the host generates replies, extracts consented
facts and supplies grounded appraisal; our tools persist and retrieve them.

```bash
.venv/bin/companion --db ./brain.sqlite3 mcp-config
```

Adapt the generated server definition to your MCP client's configuration format. It uses absolute
paths and launches `companion ... serve` over stdio. It does not alter client settings.

Suggested instruction to the connected agent:

> Use Companion Engine for this conversation. List/create my character, prepare each turn,
> retrieve original evidence when relevant, and commit the actual reply afterward. Ask before
> retaining memories unless I chose automatic memory. Use your own model for replies, notes,
> summaries and appraisal. Do not invoke paid media/embeddings without my intent.

MCP tools, including app-only UI actions, cover characters, turns/recovery, facts/corrections/deletion, evidence recall/expansion,
contextual notes, core/episode consolidation, temporal facts, relationship inspection, optional
embeddings/media, receipts, workflow/configuration discovery, playback and export/forget.

### Companion Skill and inline voice player

The packaged [Companion Skill](../src/companion_engine/plugin/skills/companion-conversation/SKILL.md)
defines entry/selection, prepare → evidence → reply → commit, review/automatic/off consent,
corrections, interrupted turns and paid-media boundaries. On entering companion mode the MCP
instructions tell the host to call `get_companion_workflow` once. This makes the workflow available
even without installing the native skill package. Reading instructions does not force tool use.

Export a native skill plugin (archive contains only authored skill resources, never DB/keys):

```bash
.venv/bin/companion plugin-package ./output/companion-engine-plugin.zip
# To associate an existing registered MCP app, add --app-id <your registered app ID>.
```

Exporting does not install/publish the plugin or update an existing ChatGPT connection. Refresh
the host's tool discovery after restarting the MCP server. Native skill installation is separate.

For a requested voice message, configure speech provider/model/key through environment variables
or the private settings form, and set `voice_id` using the SDK or character update tool.
Doubao clone 2.0 uses
`seed-tts-2.0-expressive`, an owned `S_*` speaker and the speech API key, not an ElevenLabs key.
The NDJSON stream must finish successfully; truncated audio is not saved. Host chat requires no extra reply
model key. `provider_status` reports configuration without exposing keys.

`generate_voice_message` generates once and returns an artifact; `show_voice_message` renders
an MCP Apps card. Tapping the bubble reads receipt-owned audio in bounded chunks through the existing
MCP connection. The compact voice bubble loads and plays on tap; tap again to pause. It shows
actual duration after loading, supports light/dark themes and English/Chinese, and offers Save audio.
There is no public audio URL, open local HTTP port, playback before user interaction or extra provider call for replay. `_meta` carries audio bytes
to the authorized component rather than into model-visible text. The host still receives them.
Clients without MCP Apps need their own playback integration; an artifact alone does not mean
audio was played. SDK applications can read scoped artifact bytes. The prebuilt card ships in the wheel;
only UI developers need `cd ui && npm ci && npm run build`.

Playback and failure/retry were verified in a local browser with a synthetic MP3 and host bridge,
plus real Python stdio MCP tests. Player v3 was also verified inside ChatGPT: rendering,
first-tap playback and second-tap pause with an existing MP3; see the latest host status in
[verification](verification.md). Restart the runtime and refresh ChatGPT tools after upgrading;
do not stop a tunnel unless its runtime credential is available for restart.

**MCP is not a conversation interceptor.** The host must call tools; we cannot guarantee that it
adopts a persona or saves every turn. Cloud hosts receive the returned context. ChatGPT needs a
supported private tunnel or remote deployment; it cannot directly reach laptop localhost.
See [connection modes](connections.md).

## Automatic memory maintenance

For summaries written by ChatGPT/another MCP host, use `configure_host_summaries`.
No backend summary-model API key is needed. Turn responses indicate when a bounded batch
is due; the host reads `prepare_memory_summary` and saves cited core/episode summaries with
`commit_memory_summary`. This runs during active conversations, not after closing ChatGPT.
Embedding/indexing settings and their API costs remain separate.

Use `configure_memory_automation` in MCP or the SDK memory policy to configure per-character
embedding, summary intervals and semantic retrieval. `http` and `serve` run the durable worker;
SDK users can run `with runtime.automation.worker(): ...` or a separate `companion --db PATH worker`.
Embedding indexing, semantic retrieval, and host summaries default on for characters without an explicit automation policy. Existing explicit off policies are preserved. Embedding API calls incur provider fees once an embedding provider is configured; no provider means a visible pending configuration state, not a silent substitute. Backend summary-model calls stay off. Missing providers and uncertain failed jobs are visible in
status; interrupted paid requests are never blindly retried. Background summaries need their
own configured model, even when ChatGPT writes the conversational replies.

In ChatGPT, ask to `show_memory_panel` for facts, cited summaries and processing status, with
real revision-checked edit/delete controls. Restart the runtime and refresh host tool discovery
for the new component. See [configuration, recovery and scale limits](memory-automation.md).

## Implemented memory and emotion layers

| Layer | Behavior |
| --- | --- |
| Originals | Immutable user/assistant excerpts, attribution and timestamps |
| Facts | Stable keys, exact quotes, subject, importance/confidence, dates and revisions |
| Corrections | Explicit revision checks; incorrect vs formerly-true claims |
| Notes | Retrieval descriptions/keywords; links require quotes on both original sources |
| Retrieval | English/CJK originals plus independently indexed facts/documents; optional embeddings/RRF |
| Time | User timezone; occurrence vs observation dates; paginated history/as-of with unknown validity |
| Style | Source/revision-backed expression rules, automatically deactivated on source edits/deletion |
| Recovery | Missing analysis receipts, quote-checked repair without retroactive mood changes |
| Media memory | Host-observed image/audio descriptions with explicit provenance and uncertainty |
| Reader | Bounded lookup controller (up to two extra model calls), then separate final reply |
| Organization | Citation-backed core/episode summaries, explicit resumable batches |
| Open loops | Importance/arousal/time-ranked events, suppression policy and local-date calendar context |
| Guidance | Current intent/boundaries, uncertainty, appropriate support and short attunement record |
| State | 12 appraisal labels, PAD, relationship axes, caps/decay/hysteresis/habituation |
| Access | SQLite/WAL, SDK, CLI, stdio MCP, authenticated loopback JSON API and HTTP MCP |

Notes/summaries are derived hints, not independent evidence. Quote checks prove a quote exists,
not that the interpretation is true. Emotional state is fictional simulation, **not** diagnosis
or a clinically validated assessment.

Configured embedding indexing and retrieval default on unless explicitly disabled. The worker
indexes originals and derived fact/document layers; these embedding calls can incur fees.
One query embedding is reused across the original and derived lanes. Host summaries use the
host model during active conversations; backend summary-model calls remain off by default.
Raw evidence/lexical recall need no provider. Vector search remains local exact scanning, not ANN.
Read [architecture and intentional limits](architecture.md).

## Python SDK

```python
from companion_engine import CharacterSpec, CommitRequest, Engine, MemoryDraft

with Engine("brain.sqlite3", owner="alice") as brain:
    brain.create_character(CharacterSpec(
        id="nova", name="Nova", persona="Curious and concise; respect boundaries."
    ))  # Once; inspect/update on later sessions.
    context = brain.prepare_turn(
        "nova", "Please remember I prefer tea", request_id="conversation-1-turn-1"
    )
    # Send context.model_dump(mode="json") to YOUR agent/model.
    brain.commit_turn("nova", context.turn_id, CommitRequest(
        assistant_reply="Tea it is.",  # Replace with the actual generated reply.
        memories=[MemoryDraft(
            key="preferred_drink", text="Prefers tea", category="preference",
            source_quote="I prefer tea",
        )],
    ))
    evidence = brain.memory.recall("nova", "tea")
```

Standalone apps use `Runtime(engine, Settings.from_env()).chat(character_id, message, request_id)`.
If `needs_review`, display proposals then call `approve(character_id, turn_id, retain=True/False)`.
Other entry points: `memory.annotate/expand/facts_at/consolidate`, `Runtime.maintain/index/recall`,
`Media.speech/image/transcribe`, and explicit no-network `Engine.tick` for daily mood variation.

## Reliability, privacy and cost

- Owner/character isolation is enforced in the DB. Owner is trusted local configuration, not
  public authentication. Prefer separate DBs for unrelated people.
- Atomic commits/revisions prevent duplicate effects. Runtime reserves request IDs before paid
  calls; exact successful replays reuse receipts. Ambiguous failures require explicit review and
  a new ID, not automatic paid retries. No DB lock spans network I/O.
- Usage is recorded where reported. Missing usage/prices are **unknown**, not free. No hard dollar
  spending cap is implemented; reconcile invoices. External host inference costs are not visible.
- HTTPS endpoints only, except loopback models. No arbitrary remote image URL downloading.
- Local HTTP uses bearer auth, Host/Origin checks and a strict CSP. It is not public OAuth; do not
  expose its port to the internet. OS/process access can still read local data.
- SQLite/media are not app-encrypted. Protect disk/backups/exports. Cloud APIs receive submitted
  context/audio/prompts even though our DB stays local.
- Fact deletion preserves source history. Character forget removes DB records/indexes, **not**
  generated files, backups or provider/host copies. Media lives in `<database-stem>-media/`.
- Schema v1 upgrades preserve history/facts and locally index old committed originals. Back up
  existing databases first. No external production data is imported.

## Test and scope

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests examples
.venv/bin/python examples/offline_demo.py
# HTTP/settings checks use synthetic data and make no paid calls:
.venv/bin/pytest tests/test_runtime_http_media.py tests/test_settings_card.py -q
```

See [verification](verification.md). Not implemented: live calls/video avatars, autonomous
daily life/outreach, production billing, public OAuth/multi-user hosting, cloud sync, bulk
import/restore, reference-image identity control, native Anthropic Messages adapter, or a published
ChatGPT marketplace plugin. Earlier private ChatGPT/audio integration checks are recorded in
[verification](verification.md). The 0.4 brain changes have deterministic and real local
MCP transport coverage; emotional quality and host workflow adherence need separate evaluation.

The source now includes an MIT license and release tooling. No remote or package has been
published by these local changes; see [release checks](releasing.md).

### Continuity workflow

`prepare_turn` supplies current facts, correction markers, a bounded recent user-attunement
history, and the character's fictional emotional state. The host authors the reply and
commits grounded facts/corrections, a brief attributed `attunement`, and an `appraisal`
for the character. These are separate records, not user diagnoses or hidden reasoning.

After each committed exchange the running worker indexes missing/changed originals, facts and
derived documents. Learned style and character life notebook are separate from user facts.
Host summaries become due after four turns, the character limit, idle time, or a knowledge
correction. `prepare_memory_summary` → `commit_memory_summary` lets ChatGPT write a
cited structured notebook and episode without a separate summary model. It includes
identity, people, current life, emotional landscape, motivations, open threads, dates,
shared history, tentative impressions, communication preferences, and pinned boundaries.
Empty sections are omitted. Corrections invalidate derived summaries and request a rebuild.

Host summaries still require the host to execute those tools; the MCP server cannot
force ChatGPT to act or work while the host is inactive. No historical emotional state
is fabricated when enabling continuity on an existing character. SQLite is designed
for a local owner; hosted multi-user deployment needs separate isolation/auth and capacity work.


For the source-port comparison, new MCP tools, upgrade behavior and remaining boundaries,
see [brain adaptation](brain-adaptation.md).
