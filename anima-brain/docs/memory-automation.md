# Memory maintenance and embedded management

This release completes the local scheduling path. It is not a claim of cloud-scale hosting
or of automatic access to a ChatGPT conversation.

## Data flow

1. The host calls `prepare_turn` and `commit_turn` with the actual exchange. The commit saves
   original evidence and quoted fact proposals atomically. `automatic` permits host-authored
   fact extraction; it does not intercept messages or force the host to call a tool.
2. Committed evidence without a vector/maintenance marker and unreviewed fact revisions are
   the durable backlog. There is no commit-then-enqueue gap. Notes invalidate only note vectors.
3. A policy-controlled worker claims one maintenance job per character across local processes.
   `companion http` and `companion serve` start it; SDK users use
   `with runtime.automation.worker(): ...`, or run `companion --db PATH worker`.
4. Index jobs handle up to 32 contextual chunk/lane or derived items. Background maintenance
   handles up to 20 original messages by default (2–60 configurable). It creates grounded notes,
   may clarify two linked older notes, and writes episodes, a structured notebook and optional
   character core. SDK defaults keep 10 recent exchanges until idle; direct `maintain()` flushes.
5. Reconciliation pages through older candidate facts for each new current-state/correction
   fact. Exact quotations, later observation and revision checks gate explicit transitions.
   Superseded facts remain historical; corrected errors are excluded. Invalidation forces core
   repair, including when there is no unprocessed chat backlog.
6. Original evidence uses raw/note/lexical RRF k=40. Facts and episodes use weighted relevance,
   recency and importance. The bounded reader can search again or perform typed temporal lookup.
   Query embeddings are journaled; failure is an explicit lexical fallback. Details and the
   original-source mapping are in [memory architecture alignment](memory-parity.md).

## Configuration

Use the SDK's memory policy or ask the host to call `configure_memory_automation`
after authorizing provider use:

```json
{
  "character_id": "luna",
  "policy": {
    "embedding": true,
    "summaries": false,
    "host_summaries": true,
    "semantic_retrieval": true,
    "summary_turns": 4,
    "summary_chars": 12000,
    "idle_seconds": 300,
    "daily_jobs": 100
  }
}
```

For raw Runtime/MCP characters without an explicit policy, embeddings, semantic retrieval and
host summaries default on. New SDK characters instead use background summaries and
reconciliation, with a configured writer. Existing explicit off settings remain off.
Once an embedding provider is configured, indexing can process previously saved history and
incur API fees. Host summaries require the active host to call the summary tools. Policies persist in the same SQLite database and are shared by MCP
and SDK/API clients. Memory mode `off` pauses worker processing. Disabling a switch prevents new
jobs; it does not cancel a provider request already in flight.

Provider settings stay in the existing private local provider configuration, never in MCP
responses. Embedding uses its configured endpoint/model/key. Autonomous summaries need a
configured chat model or separate summary model; the ChatGPT subscription cannot run backend jobs.
Missing configuration is reported and does not repeatedly attempt calls. Existing running
processes need restart after provider configuration is saved from another process.

The daily cap is a rolling 24-hour **job count**, not a dollar budget. One summary job may
contain up to `summary_batch_messages` note calls, optional note-query embeddings and one
consolidation call. A reconciliation job may call a semantic candidate search and a conflict writer. Chat/query-embedding and manual
operations are separate. Usage is journaled; unavailable token prices are not treated as zero.

## Recovery and correctness

A worker renews its lease while making bounded provider requests. Another process cannot
claim work for that character. A dead lease or provider/validation failure moves the job to
`review` and stops automatic maintenance for that character. There are no blind paid retries.
After inspecting receipts, the user can explicitly acknowledge the job with
`retry_memory_job`. This permits a new attempt, which may incur another charge. Pending work
survives restart. Successfully stored vectors/processed batches are not resubmitted.

SQLite serializes reservations/writes; network calls run outside transactions. Manual and
automatic index/maintenance operations share an operation reservation check. Host-authored
annotations and fact edits remain possible; revision/source checks reject stale derived writes.

## Optional MCP memory card

`show_memory_panel` renders a bundled MCP Apps component: paginated facts (50 per page),
latest eight core/episode documents, citation expansion, and actual maintenance status.
Editing/deleting uses app-visible revision-checked tools. Concurrent edits fail instead of
silently overwriting. Delete requires a second confirmation in the component. Original chat
is retained; cached summaries are cleared. This is fact deletion, not erasure of the original
information from all history, backups or model context.

Providers must be configured locally. Clients without MCP Apps can use the ordinary
inspection tools; the engine does not require the embedded UI.

## Scale boundary

Appropriate for a local owner with multiple characters and multiple local clients. No load-test
capacity claim is made. Exact vector retrieval currently scans this character's vectors:
O(number of vectors × dimensions), though only bounded top results are retained. Large corpora
need an indexed vector backend. Multi-user hosting additionally needs authenticated tenant
binding, a server database, independent workers, quotas, backups and operational monitoring.
The interfaces separate tools/UI, orchestration, storage and providers; the storage code is
still SQLite-specific and migrating it requires implementation, not just an environment flag.

## Summaries by ChatGPT / another MCP host

The user can choose `configure_host_summaries(character_id, enabled=true)`. This enables
`host_summaries` and disables autonomous summaries/reconciliation, preserving embedding settings.
Both summary writers cannot be enabled simultaneously. No backend reply-model key is needed.
Prepare/commit responses include a due notice based on the same turn/text/idle thresholds.
The host reads `prepare_memory_summary`, authors episode/core with supplied original citations,
and calls `commit_memory_summary`. Snapshot checks reject changed facts/core/batches; exact
retries are idempotent. Originals, summaries, completion markers and receipts remain local.
The host can author notes and linked updates with `prepare_note_evolution` /
`commit_note_evolution`, and inspect/apply knowledge changes with
`prepare_knowledge_reconciliation` / `commit_knowledge_reconciliation`. These make no backend
LLM calls. `annotate_evidence` remains available for direct single-note edits.

At most one bounded batch per conversational turn is recommended. This is a tool workflow,
not a guarantee that every host will follow it. It runs only when the host is active and calls
the tools; an idle threshold does not wake ChatGPT. It uses host context/usage but makes no
backend summary-model request. Embeddings remain separate and may still cost money if enabled.
The SDK's reply model does not automatically act as this MCP host.
