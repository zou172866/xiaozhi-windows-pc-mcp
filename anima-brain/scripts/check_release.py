"""Check allowlisted release inputs and built archives; never prints secret contents.

This is a local guardrail, not a guarantee that arbitrary credentials can be detected.
Run before staging/publishing. Private DBs/output may exist locally but cannot enter a release.
"""

import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {"output", "runs", "exports", "node_modules", ".venv", ".git", "__pycache__"}
PATTERNS = [
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"/Users/Chenjunyu/"),
]


def inspect(name, content):
    path = Path(name)
    private = any(p in PRIVATE_PARTS or p.endswith("-media") for p in path.parts)
    private |= path.name.startswith(".env") or ".sqlite" in path.name
    private |= path.suffix in {".db", ".pem", ".key"}
    # The scanner itself contains the forbidden personal-path pattern, not a deployment path.
    secret = any(p.search(content) for p in PATTERNS) if path.name != "check_release.py" else False
    return [name] if private or secret else []


def main():
    problems = []
    files = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    for name in filter(None, files):
        path = ROOT / name
        if path.is_symlink():
            problems.append(name + " (symlink)")
        elif path.is_file():
            if path.stat().st_size > 5 * 1024 * 1024:
                problems.append(name + " (>5 MB)")
            problems.extend(inspect(name, path.read_bytes()))
    for path in sorted((ROOT / "dist").glob("*.whl")):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                problems.extend(inspect(name, archive.read(name)))
    for path in sorted((ROOT / "dist").glob("*.tar.gz")):
        with tarfile.open(path) as archive:
            for entry in archive.getmembers():
                if entry.issym() or entry.islnk():
                    problems.append(entry.name + " (symlink)")
                elif entry.isfile():
                    problems.extend(inspect(entry.name, archive.extractfile(entry).read()))
    if problems:
        raise SystemExit(
            "Review release inputs (contents withheld):\n" + "\n".join(sorted(set(problems)))
        )
    print("Release inputs and existing wheel/sdist archives passed private-file/pattern checks.")


if __name__ == "__main__":
    main()
