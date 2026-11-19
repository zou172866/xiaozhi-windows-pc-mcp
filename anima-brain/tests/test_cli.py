import json
import subprocess
import sys
from pathlib import Path


def test_cli_lifecycle(tmp_path):
    def run(*args, success=True):
        command = [
            sys.executable,
            "-m",
            "companion_engine",
            "--db",
            str(tmp_path / "cli.db"),
            *args,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert (result.returncode == 0) == success, result.stderr
        return json.loads(result.stdout if success else result.stderr)

    run("init")
    config = run("mcp-config")
    assert config["mcpServers"]["companion-engine"]["command"] == sys.executable
    assert run("doctor")["integrity"] == "ok"
    run("create", "nova", "--name", "Nova", "--persona", "Curious, kind")
    assert len(run("list")) == 1
    ctx = run("prepare", "nova", "Please remember I prefer tea", "--request-id", "one")
    fixture = Path(__file__).parents[1] / "examples" / "commit.json"
    result = run("commit", "nova", ctx["turn_id"], "--file", str(fixture))
    assert run("memory", "search", "nova", "tea")[0]["id"] == result["memory_ids"][0]
    assert run("usage", "nova")["attempts"] == 0
    assert run("export", "nova")["format"] == "companion-engine-export"
    error = run("draft", "nova", success=False)
    assert error["error"] == "ValueError"
    run("forget-character", "nova", "--confirm", "nova")
    assert run("list") == []


def test_offline_example_runs():
    example = Path(__file__).parents[1] / "examples" / "offline_demo.py"
    result = subprocess.run(
        [sys.executable, str(example)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert "Demo complete" in result.stdout


def test_developer_entry_points_have_no_chat_frontend():
    from companion_engine.cli import parser

    commands = parser()
    assert commands.parse_args(["http"]).port == 8765
    assert commands.parse_args(["settings"]).command == "settings"
    assert "web" not in commands._subparsers._group_actions[0].choices
    example = Path(__file__).parents[1] / "examples" / "chat.py"
    result = subprocess.run(
        [sys.executable, str(example), "--help"], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0
    assert "--web" not in result.stdout and "--port" not in result.stdout
