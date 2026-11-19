# Brain adaptation

**Memory alignment update:** see [the current execution paths, defaults and source mapping](memory-parity.md).
The sections below describe the original host adaptation; the alignment update adds contextual
raw/note vectors, weighted archive retrieval, background cross-key reconciliation and old-note evolution.

## Historical 0.4 host integration

The original 0.4 release adapted the owner's `makima.io` memory/emotion implementation to a local,
host-driven Python engine. It does not import Convex or copy Rina-specific personality,
old Makima drift-scoring preferences, production credentials, or application billing.

## Implemented paths

| Concern | Local implementation | Model responsibility |
| --- | --- | --- |
| Knowledge changes | Indexed state-key candidate lookup plus lexical/semantic fact search; CAS corrections distinguish false claims from formerly true claims; source invalidation markers accompany original recall | Decide whether the same subject/attribute actually conflicts. Similarity never authorizes overwriting. |
| Retrieval | Original evidence, facts and derived documents have separate indexes. Atomic SQLite triggers cover all writers and deletes. One turn-query embedding is reused across lanes. Old episodes are searchable beyond the recent-doc window. | Inspect original citations; do not treat summaries as proof. |
| Time | `lookup_memory_time`: current, history, as-of, events, observations; user timezone; paginated results; uncertain validity returned separately | Resolve relative dates in the supplied timezone or ask; no hidden planner model call. |
| Response style | Grounded prefer/avoid/perception/response/intensity/example rules with source IDs/revisions. Stale rules stop applying when source memories change or disappear. | Compile lasting preferences; current requests override rules. One-turn preferences stay in attunement. |
| Attunement | Optional alternative interpretation, character reaction and responsive move; explicit-phrase feedback linked to prior turns and returned with prior interpretations | Keep interpretations tentative and grounded. Feedback is not automatic emotional truth. |
| Character state | Existing PAD/relationship physics, plus daily weather seeded once during ordinary preparation; state transitions retained for inspection | Submit a live appraisal with the turn, including neutral deltas when appropriate. |
| Recovery | Durable missing-analysis receipts and idempotent quote-checked repair; summary due/backlog remains visible | Repair from original turns. Historical annotations never retroactively apply mood deltas. |
| Character self-memory | Host summary commit can atomically update a separate cited `character_core` | Maintain established fictional events, dated arcs and mood separately from fixed persona and user facts; do not invent daily events to fill the field. |
| Dates/open loops | Annual dates, gentle/celebrate/deadline handling, local-date context and importance/arousal/age-ranked open loops | Bring up only contextually appropriate, unsuppressed topics. No notification/outreach scheduler. |
| Visual/audio evidence | Optional host-observed media on prepare, immutable source reference/observation, quote/confidence validation, separate host-observation provenance in facts and notebooks | Actually perceive the attachment first. No backend download or independent image verification is implied. |
| Notes | Existing exact-quote links and revision-checked edits; returned note metadata supports revising old descriptions after clarification | Preserve old source meaning; add new events separately. |

User-fact changes rebuild the current core and invalidate dependent episodes, preserving
unrelated episodes when their original dependency is known. Manual/unattributed corrections
still invalidate conservatively. Original conversations are retained and labelled with
knowledge-update markers; deleting a fact is not transcript erasure.

## Host flow

1. Read `get_companion_workflow`; prepare the real user message, plus observed media if applicable.
2. Use provided memories, calendar, learned style, feedback and current character state.
3. If evidence is missing, use layered recall, expand original sources, or time-scoped lookup.
4. Inspect relevant old facts before committing a changed state; never make a competing key to
   evade revision checks. Commit the actual reply, grounded facts/corrections, attunement and appraisal.
5. Process due host summaries and missing-analysis receipts with the host model. Compile style
   changes when lasting feedback warrants them; update self-memory only when supported.

Host mode remains a cooperative protocol. The backend cannot force ChatGPT to call a tool,
see unrelated chats, or summarize while the host/computer is inactive. Background summary
model calls remain disabled by default in this host-only mode. New standalone SDK characters
enable configured background memory maintenance; see [current defaults](memory-parity.md#defaults-and-how-to-run-them).
Existing explicit automation-off choices are preserved.

## New tools

- `review_memory_update` — candidate retrieval; configured semantic calls may incur embedding fees.
- `lookup_memory_time` — local paginated temporal lookup, no provider call.
- `inspect_continuity_tasks` / `complete_turn_analysis` — inspect and repair missing records.
- `inspect_response_style` / `update_response_style` — inspect and compile grounded style.

Existing recall tools now expose derived layers alongside originals. `prepare_turn` accepts
optional `media_observations`; `commit_memory_summary` accepts optional `character_core` and
`character_source_ids`. Existing payloads remain valid. The six new tools require host tool
rediscovery after the runtime loads the new code.

## Storage and costs

Migration adds tables/indexes/triggers and backfills search indexes locally. It does not extract
new facts, invent old emotional states or call providers. Existing source records are preserved.
The SQLite schema compatibility number remains 2; the additions are idempotent.

Configured workers now embed changed/unindexed facts and documents as well as originals.
This adds embedding work; no extra chat/summary API key is required in host mode. Edits invalidate
only stale vectors; paid-operation receipts and uncertain-failure handling remain in place.
Derived vectors use binary float storage. Search is still scoped exact cosine with bounded heaps,
not an ANN index or a validated large-scale hosted service. Source selection and summary context
are bounded with explicit omissions; they do not establish exhaustive recall.

## Remaining boundaries

- No autonomous life simulation, outreach or live calls are enabled by this change.
- No automatic background LLM reconciliation when the user has chosen host-only reasoning.
- No claim of perfect conflict detection, clinical emotional measurement, or improved LLM quality
  from code tests. Dedicated comparative conversations/benchmarks are still needed.
- No public multi-user auth, cloud sync, distributed scheduler or hosted-scale guarantee.
- Media references are host-supplied provenance; raw attachment bytes are not automatically stored.
- Stored real-world factual records and simulated character emotion remain separate.
