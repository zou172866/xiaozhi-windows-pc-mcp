"""Local host exports. No database access, credential copying or host settings edits."""

import json
import shlex
import sys
from pathlib import Path


def mcp_config(database, owner, *, settings_file=None):
    env = {"COMPANION_ALLOW_CHAT_CREDENTIALS": "0"}
    if settings_file is not None:
        env["COMPANION_SETTINGS_FILE"] = str(Path(settings_file).expanduser().absolute())
    return {
        "mcpServers": {
            "companion-engine": {
                # Do not resolve this symlink: doing so can bypass the virtualenv.
                "command": str(Path(sys.executable).absolute()),
                "args": [
                    "-m",
                    "companion_engine",
                    "--db",
                    str(Path(database).expanduser().absolute()),
                    "--owner",
                    owner,
                    "serve",
                ],
                "env": env,
            }
        }
    }


def export_hosts(output, database, owner, *, settings_file=None):
    """Export both hosts from the same server definition and canonical skill."""
    root = Path(output).expanduser().absolute()
    config = mcp_config(database, owner, settings_file=settings_file)
    server = config["mcpServers"]["companion-engine"]
    source = Path(__file__).parent / "plugin"
    metadata = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    manifest = {key: metadata[key] for key in ("name", "version", "description")}
    manifest["description"] = (
        "Persistent character memory and emotional continuity through local MCP."
    )
    patch = [
        {
            "insert": [
                {
                    "id": "memory-companion-engine",
                    "name": "@deepseek-ai/dsh-mcp-client",
                    "config": {"serverName": "companion_engine", "transport": "stdio", **server},
                }
            ]
        }
    ]

    def encode(value):
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    files = {
        ".claude-plugin/marketplace.json": encode(
            {
                "name": "companion-local",
                "owner": {"name": "Companion Engine"},
                "plugins": [{"name": "companion-engine", "source": "./claude"}],
            }
        ),
        "claude/.claude-plugin/plugin.json": encode(manifest),
        "claude/.mcp.json": encode(config),
        "claude-desktop-mcp.json": encode(config),
        # JSON is a YAML subset; avoids hand-escaping paths in a Cordis YAML overlay.
        "deepseek/companion.cordis.yml": encode(patch),
        "deepseek/package.json": encode(
            {
                "name": "companion-engine-dsh",
                "version": metadata["version"],
                "private": True,
                "description": "Persistent companion characters, memory and emotional continuity.",
                "dsh": {"bundle": {"patch": "./companion.cordis.yml"}},
                "files": ["companion.cordis.yml", "locale", "README.md"],
            }
        ),
        "deepseek/locale/en.json": encode(
            {
                "meta": {
                    "title": "Companion Engine",
                    "description": (
                        "Characters, persistent memory and emotional continuity via local MCP."
                    ),
                }
            }
        ),
        "deepseek/locale/zh.json": encode(
            {
                "meta": {
                    "title": "Companion Engine",
                    "description": "本地角色、长期记忆与情绪延续。通过 MCP 接入现有 Python 引擎。",
                }
            }
        ),
        "deepseek/README.md": (
            "# Companion Engine\n\n"
            "Adds local character memory and emotional continuity tools. "
            "Ask the agent to call get_companion_workflow and list_characters.\n\n"
            "Requires the Python installation at the path in companion.cordis.yml. "
            "Memory stays in the configured local database; retrieved context is sent "
            "to your host model. Optional configured media/embedding providers may bill.\n\n"
            "Disable this bundle in Plugins to disconnect tools without deleting memory. "
            "Do not also load the same file through --patch: that duplicates the bridge.\n"
        ),
    }
    for path in (source / "skills").rglob("*.md"):
        if not path.is_symlink() and path.is_file():
            files["claude/" + path.relative_to(source).as_posix()] = path.read_text("utf-8")
    files["START-HERE.md"] = f"""# Local Companion Engine host setup

These files use this machine's Python installation and database path. Regenerate
on another machine. No credentials, database contents or transcripts are bundled.
Database: `{server["args"][3]}`; owner: `{owner}`.

## Claude Code

```sh
claude plugin validate {shlex.quote(str(root / "claude"))}
claude plugin marketplace add {shlex.quote(str(root))}
claude plugin install companion-engine@companion-local
```

Approve the local MCP connection when Claude asks; use `/mcp` to inspect it.
The included companion-conversation skill reuses the engine's canonical workflow.
Find it in `/plugin` → Installed. Restart existing Claude Code sessions after installing.
If you previously registered a standalone `companion-engine` MCP server, remove
that duplicate only after confirming the plugin is enabled.

## Claude Desktop

Merge the `companion-engine` entry from `claude-desktop-mcp.json` into
`mcpServers` in your Desktop configuration. Preserve existing entries and restart
Claude Desktop. On macOS the file is
`~/Library/Application Support/Claude/claude_desktop_config.json`.
This is a local server configuration, not a Claude web connector or .mcpb archive.

## DeepSeek Harness

Use Node >=24.9 and `@deepseek-ai/dsh@0.2.2-rc.1` (the integration baseline).
Install with `npm install -g @deepseek-ai/dsh@0.2.2-rc.1`, then:

```sh
dsh plugin --profile web add {shlex.quote(str(root / "deepseek"))}
dsh web
```

Find Companion Engine in Harness's Plugins page. Do not also pass the old
`--patch` option when the bundle is installed. Configure the model in Harness itself.
The bundle only adds the MCP
bridge; it does not replace Harness's model or system instructions. Tool names
start with `mcp__companion_engine__`. Never overwrite an existing Cordis patch.

## Start a companion conversation

Tell the host: "Use Companion Engine for this conversation. Read
get_companion_workflow, list my characters, and help me select or create one.
Use your own model for replies and summaries, prepare each actual user turn,
then commit the exact reply with grounded memory and appraisal."

In a fresh host session, select the same character and ask it to recall a fact
saved in the previous session. Check the tool calls, not just the fluent reply.
Both exports point to the SAME database and owner. Do not chat simultaneously
with the same character in both hosts: one pending turn per character is allowed.
For independent memories, export again with another `--db` or `--owner`.

The runtime must remain installed at the exported Python path. Memory is local,
but retrieved content is sent to the selected host model. Host instructions
guide tool use; MCP alone cannot force every turn to be saved. Inline media
rendering depends on the host; hosts without MCP Apps need their own playback integration.

Local provider settings remain outside this package. Configured embedding or
maintenance providers may make API calls when the server runs. An unconfigured
engine works with lexical recall and host-written summaries. Chat credential
entry is disabled by this configuration; use environment variables or the private
provider-settings form (`companion settings`, with the `http` extra installed).
"""
    # Exclusive directory creation protects previous exports and user configuration.
    root.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {"directory": str(root), "files": sorted(files), "database_initialized": False}
