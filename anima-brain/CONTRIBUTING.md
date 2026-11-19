# Contributing

Python 3.11+ and SQLite FTS5 are required. Work from a source checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,http]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests examples scripts
.venv/bin/python examples/offline_demo.py
```

Use synthetic people and conversations in tests. Tests must not load real credentials,
call paid services or inspect a developer's database. The fixtures isolate provider
settings; HTTP tests use `httpx.MockTransport`. Live checks are explicit opt-in examples.

Keep the main API in `brain.py` a thin wrapper around `Engine` and `Runtime`. Preserve
request replay, owner/character scoping, quote provenance and transactional commits.
Test retries, failures and persistence for changes that affect those guarantees.
Do not silently fall back to a different model, provider, owner or memory policy.

The Python wheel is headless. The `ui/` directory contains only MCP Apps cards.
The optional standalone web example lives in `examples/showcase/`; see its
[development and browser checks](examples/showcase/README.md#development-and-verification).
To modify those assets, use
`cd ui && npm ci && npm run build`, then include updated prebuilt assets and notices.
Keep package/plugin version metadata aligned when cutting a release.

For README imagery, follow the [visual asset guide](docs/assets/README.md).
Keep public Anima brand material separate from the MIT-licensed code; do not add
private user media or imply that these characters ship with the SDK.

Before sharing: follow [release checks](docs/releasing.md). Do not add real prompts,
transcripts, private generated media, `.env` files or local host exports to commits. Describe
the behavior change and verification in the pull request; separate quality claims
from transport/contract test results.

## README languages

Keep [English](README.md) and [Simplified Chinese](README.zh-CN.md) aligned when changing
setup commands, capabilities, defaults or limitations. Localize explanations and examples
for the reader rather than translating sentence by sentence. Preserve API identifiers,
environment-variable names and executable commands. Both READMEs share the same assets
and link to the current technical guides; those guides are not yet localized.
Keep the language links reciprocal and include each README in the source distribution.
