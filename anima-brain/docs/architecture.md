# Architecture and port boundaries

For the illustrated walkthrough, start with [How the companion brain works](brain-flow.md).
[Live calls](live-calls.md) reuse this runtime for streaming voice and animated video.

```text
Existing agent ── stdio / local HTTP MCP ─┐
Python code ───────── SDK ────────────────┼─ Engine + MemorySystem ─ SQLite/FTS5
HTTP client ─ API ─ Runtime ──────────────┘
                       └─ optional BYOK providers + private media files
```

Host mode uses the existing agent's own model. Runtime provides standalone orchestration.
Core storage/emotion modules do not depend on MCP or Starlette; no sibling repo is imported.
Optional MCP cards and an isolated provider-settings form ship in the Python wheel;
the optional standalone React showcase lives separately in `examples/showcase/`.

## Lifecycle

Prepare reserves an idempotent pending turn and assembles persona, boundaries/style, corrections,
recent history, core/episode summaries, open loops and original evidence. Standalone Runtime may
run up to two lookup decisions, then a separate final reader. The host can do this itself instead.
Malformed/failed planner output cannot become evidence or replace the reply.

The reply contains structured fact/correction/appraisal proposals with quotes in the current
user message (or explicitly registered, uncertain host media observations). New characters default
to automatic memory; explicit review mode waits for approval; off mode saves history
but rejects fact writes. A single transaction stores the actual reply, originals, facts/revisions
and state. One pending turn per character prevents silent ordering races. Exact retries have no
extra effects. Paid operations are reserved/journaled outside network calls and never auto-retried.

## Memory semantics and limits

- Originals retain speaker, timestamp and stable ID; consolidation never removes them.
- Facts carry subject, temporal validity, importance/confidence, emotional metadata and source.
- Explicit corrections use revision checks. False claims do not reappear in as-of history;
  formerly true claims may. Unknown dates stay unknown. Cross-key semantic conflicts still need
  model/human judgment; no perfect contradiction detection is claimed.
- Contextual notes: <=600-character descriptions, eight keywords, three typed links. Exact quotes
  on both originals ground links, but do not prove semantic entailment.
- Raw contextual and note contextual embeddings are separate from facts/documents. Evidence
  uses RRF k=40; archive facts/episodes use relevance + recency + importance. Note edits invalidate
  only the note lane. Namespaced exact cosine scans retain bounded results, not an ANN index.
- Expansion returns linked and neighboring originals, preserving attribution. Recall date filters
  mean observation time; fact/event validity is separate.
- Background maintenance handles up to 20 originals per batch by default (configurable 2–60):
  new/old note evolution, then an atomic episode/core notebook and optional character core.
  Processed markers differ from citations, so uncited acknowledgements do not loop forever.
  Core retains <=40 cited originals; old originals/episodes remain available. Existing core
  citations are re-presented as originals, not treated as proof by themselves.
- Host batches bound originals and optional fact context, reporting omissions explicitly;
  unusually large mandatory context fails rather than truncating evidence silently.
  Context uses whole entries within 48k characters by default, not token counting; mandatory
  overflow fails, optional omissions are counted. These are bounded-context limits, not deletion.
- Core stays pinned before recent episodes. Corrections/deletions invalidate derived summaries
  conservatively. Suppressed follow-up is not transcript erasure. No scheduled outreach is included.

SQLite/WAL suits local use. Hosted scale needs a storage/search adapter, verified identity,
queues/quotas and separate load/security tests. SQL is not advertised as an interchangeable
storage interface. Do not add distributed infrastructure just to run one local agent.

## Emotion and grounding

Adapted from production: all 12 appraisal labels/PAD coordinates, relationship axes, trust loss
aversion, warmth habituation, daily movement caps, 12h mood/72h tension decay, stage hysteresis,
familiarity-gated romance and explicit seeded daily mood variation. Replies prioritize present
intent/refusal/correction and uncertainty over historical state. Short attunement records contain
intent/cue/uncertainty, not hidden chain-of-thought.

Intentional differences: generic characters, zero/opt-in romance baseline, daily weather seeded
once during normal turn preparation,
and time-partition-invariant long-absence warmth easing rather than repeatedly applying a full
absence. Model appraisals remain fallible. This is neither therapy nor a validated psychological
measurement, and quality has not been benchmarked in this new repo.

## Source provenance and non-parity

Reference: owner's `makima.io/convex/emotion.ts` (including numerical fixtures),
`memoryNotePolicy.ts`, `memoryReaderPolicy.ts`, `memoryTemporal.ts`,
`docs/memory-reader-production.md`, selected `memoryActions.ts` extraction and `memory.ts`
core/episode/open-loop interfaces. Existing guidance draws on `innerThought.ts`/`styleLayer.ts`
concepts. No private assets, research datasets or credentials were copied. Public Anima
branding and character artwork appear only in repository documentation, not as SDK characters.

Not a complete production port: autonomous life/outreach, the production cloud scheduler,
some historical import formats, a fact-to-fact entity graph, exhaustive conflict resolution
and hosted auth/billing remain outside this SDK. Optional live-call transports are documented
separately in [live calls](live-calls.md). Configured indexing runs under the persisted automation
policy. SDK summary and reconciliation workers use the configured writer; host-authored
summary/style/self-memory workflows require host tool execution.
See [memory architecture alignment](memory-parity.md) for executable paths, defaults and test boundaries.

## Provider and security boundaries

Chat requires JSON-object Chat Completions; native Anthropic APIs need another adapter. Embedding,
image and transcription use compatible HTTP contracts. ElevenLabs/MiniMax/Doubao speech are explicit
adapters. Actual model support, account permission and quality need authorized live testing.
The shared media HTTP collector normalizes encoding/length headers after decompression;
compressed provider responses must not be decoded a second time. Doubao clone 2.0 accepts
only a complete successful NDJSON audio stream, with bounded base64 MP3 and no automatic retry.

Image input accepts validated local files or bytes, not arbitrary remote URLs. A separately
configured vision model produces uncertain observations for the text reader. Image generation
exposes typed size, quality and count options; reference-image edits are explicit and require a
compatible provider. Generated assets are stored locally. See [SDK capabilities](sdk-capabilities.md)
for formats, limits and reference handling. One-shot speech and transcription are explicit
operations; dictation produces editable text. Streaming calls use the separate call SDK.
Actual usage is logged when provided;
timeouts may still be billed. Missing prices are unknown, not zero; no hard budget cap.

Loopback HTTP checks Host/Origin and bearer token, serves a strict CSP, and has one configured
owner. It is not OAuth/multi-user auth. Never expose this port publicly. The separate provider
form applies settings to its own process and redacts secrets on reads. Explicit saving writes a
mode-0600 non-secret settings file outside the repo plus macOS Keychain for secrets on macOS.
Other platforms use an unencrypted private-file fallback. Environment overrides apply on restart.
The ChatGPT settings card has no credential inputs; its app-only button opens a token-protected
settings-only loopback page on the engine computer, sharing the MCP Runtime without a
duplicate worker. It cannot access chat, characters, media or MCP endpoints.
Separately launched processes still require restart. The local token never enters MCP output.
The inline voice component uses MCP Apps UI and app-only chunk reads; every read checks the
same owner/character receipt, requires an MP3 artifact, rejects symlinks, and returns bounded
audio in UI-only result metadata. No public file server is introduced. Playback cannot call TTS.
The portable skill is served by a read-only tool as well as exported in a native plugin archive;
installation/runtime reload is not automatic. SQLite/media are unencrypted at app level.
Deleting facts keeps source transcripts; character forget cascades DB/indexes but not generated
files/backups/provider copies. Public release requires license/provenance/security review.
