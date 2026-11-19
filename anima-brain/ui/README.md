# MCP Apps cards

This directory contains optional **inline components for MCP hosts**, not a chat website.
The standalone Companion Engine chat frontend has been removed.

- `voice-player.*`: plays an existing authorized audio artifact inside the host.
- `memory-panel.*`: inspects and edits cited memories through MCP tools.
- `provider-settings.*`: shows provider readiness and opens local credential setup.
- `host-theme.*`: matches the host's theme.

Rebuild after changing these sources:

```bash
cd ui
npm ci
npm run build
```

The prebuilt HTML and dependency notices ship in the Python package. SDK users do not
need Node. The separate minimal provider form lives in
`src/companion_engine/static/local-settings.*`; it contains no chat features.
