# Source release checklist

This repository can be installed from source; no public PyPI package or remote release
is assumed by the quickstart. Publishing remains a separate maintainer action.

```bash
python -m pip install -e '.[dev,http]' build
pytest -q
ruff check src tests examples scripts
python examples/offline_demo.py
python -m build
python scripts/check_release.py
```

The wheel includes only `src/companion_engine` and license metadata. The source archive
uses an explicit allowlist. Local `output/`, host exports, DB/WAL files, media, environments,
Node modules and credentials are excluded. The scanner checks tracked and untracked
non-ignored files plus existing archives, reporting paths without printing secret values.
It is a guardrail, not a comprehensive secret detector. Review `git status --short` and
the staged diff before committing. Never use a filesystem archive of the whole workspace.

Create a fresh virtual environment outside the checkout, install the built wheel with
no extras, run `pip check`, import `CompanionBrain`, and run the quickstart. Then install
`[http]` and verify the authenticated API and provider-settings form. The SDK and prebuilt
MCP cards must work without Node or access to this checkout. Check that removed chat assets
are absent from both archives. Use synthetic messages only for smoke
tests, and report provider/model, token usage, restart recall and failure outcomes separately
from unit tests. Do not claim companion quality from a successful API call.

Before first public publication:

1. Confirm MIT is the intended license and contributors have rights to their contributions.
   Preserve bundled dependency notices and check any newly added assets.
2. Choose the public repository/package namespace, add its URLs to package metadata, and
   enable private vulnerability reporting or a maintained security contact.
3. Verify CI in the public repository. Local passing tests are not a completed CI run.
4. Review generated archives, install from the artifacts, then publish/tag explicitly.

Current status and concrete local checks are recorded in [verification](verification.md).
