"""Unified 3-layer test runner for xiaozhi-server (P2-24, H-8).

Layers:
  - unit:     Pure offline unit tests (host pc-mcp + anima-brain + ephemeral `docker run --rm` container suites)
              Zero dependency on any already-running container!
  - contract: Upstream xiaozhi-esp32-server image contract & patch signature tests (ephemeral `docker run --rm`)
  - live:     Live end-to-end integration tests across running 18000 / 18003 / 18080 / 18787 / 18900 / 19880
  - all:      Runs unit + contract + live and prints a unified summary table

Usage:
  python scripts/run_tests.py --layer unit
  python scripts/run_tests.py --layer contract
  python scripts/run_tests.py --layer live
  python scripts/run_tests.py --layer all
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

SERVER_DIR = Path(__file__).resolve().parent.parent
APP_DIR = SERVER_DIR / "app"
TESTS_CONTAINER_DIR = SERVER_DIR / "tests" / "container"
CONFIG_DIR = SERVER_DIR / "config"
DATA_SCRIPTS_DIR = SERVER_DIR / "data"
VOICES_DIR = SERVER_DIR / "voices"
PC_MCP_DIR = SERVER_DIR / "pc-mcp"
PC_MCP_PYTHON = PC_MCP_DIR / ".venv" / "Scripts" / "python.exe"
ANIMA_DIR = SERVER_DIR / "anima-brain"
COMPOSE_FILE = SERVER_DIR / "docker-compose.yml"

_DEFAULT_UPSTREAM_IMAGE = (
    "ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest"
    "@sha256:cececd7752e00e044bf9f099751a140834053573bbe6dd55ecbcedcb20c19c25"
)
_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+)s")
_SKIP_RE = re.compile(r"\((?:[^()\n]*,\s*)?skipped=(\d+)\)")
_IMAGE_RE = re.compile(
    r"^\s*image:\s*(?:\$\{[A-Za-z_][A-Za-z0-9_]*:-)?([^\s}]+xiaozhi-esp32-server[^\s}]+)\}?\s*$",
    re.MULTILINE,
)


def resolve_upstream_image() -> str:
    """Resolve pinned xiaozhi-esp32-server image ref from env override or docker-compose.yml."""
    override = (os.environ.get("XIAOZHI_TEST_IMAGE") or "").strip()
    if override:
        return override
    if COMPOSE_FILE.is_file():
        raw = COMPOSE_FILE.read_text(encoding="utf-8", errors="replace")
        m = _IMAGE_RE.search(raw)
        if m:
            return m.group(1).strip()
    return _DEFAULT_UPSTREAM_IMAGE


def _ephemeral_container_cmd(test_filenames: str | list[str] | tuple[str, ...], *, image: str) -> list[str]:
    """Build an ephemeral `docker run --rm` command (H-8 / P3-2: zero dependency on running containers).

    Accepts one or multiple test filenames; when multiple files are passed, executes each in an
    isolated Python subprocess with freshly reset `/tmp/xz_test_state` and `.config.yaml` inside
    a single container startup (~3x faster offline regression).
    """
    files = [test_filenames] if isinstance(test_filenames, str) else list(test_filenames)
    if len(files) == 1:
        bootstrap_sh = (
            "mkdir -p /opt/xiaozhi-esp32-server/data /opt/xiaozhi-esp32-server/state /tmp/xz_test_state && "
            "cp /opt/xiaozhi-esp32-server/config_templates/.config.example.yaml /opt/xiaozhi-esp32-server/data/.config.yaml && "
            f"exec python3 /opt/xiaozhi-esp32-server/tests/{files[0]}"
        )
    else:
        joined = " ".join(files)
        bootstrap_sh = (
            "set -e; "
            "mkdir -p /opt/xiaozhi-esp32-server/data /opt/xiaozhi-esp32-server/state; "
            f"for tf in {joined}; do "
            "rm -rf /tmp/xz_test_state && mkdir -p /tmp/xz_test_state && "
            "cp /opt/xiaozhi-esp32-server/config_templates/.config.example.yaml /opt/xiaozhi-esp32-server/data/.config.yaml && "
            'python3 "/opt/xiaozhi-esp32-server/tests/$tf"; '
            "done"
        )
    cmd = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "-e",
        "TZ=Asia/Shanghai",
        "-e",
        "XIAOZHI_USING_EXAMPLE_CONFIG=1",
        "-e",
        "XIAOZHI_APP_DIR=/opt/xiaozhi-esp32-server/app",
        "-e",
        "XIAOZHI_CONFIG_DIR=/opt/xiaozhi-esp32-server/data",
        "-e",
        "XIAOZHI_STATE_DIR=/tmp/xz_test_state",
        "-e",
        "XIAOZHI_VOICES_DIR=/opt/xiaozhi-esp32-server/voices",
        "-e",
        "XIAOZHI_HOST_SCRIPTS_DIR=/opt/xiaozhi-esp32-server/host_scripts",
        "-v",
        f"{APP_DIR}:/opt/xiaozhi-esp32-server/app:ro",
        "-v",
        f"{TESTS_CONTAINER_DIR}:/opt/xiaozhi-esp32-server/tests:ro",
        "-v",
        f"{CONFIG_DIR}:/opt/xiaozhi-esp32-server/config_templates:ro",
    ]
    if DATA_SCRIPTS_DIR.is_dir():
        cmd.extend(["-v", f"{DATA_SCRIPTS_DIR}:/opt/xiaozhi-esp32-server/host_scripts:ro"])
    if VOICES_DIR.is_dir():
        cmd.extend(["-v", f"{VOICES_DIR}:/opt/xiaozhi-esp32-server/voices:ro"])
    cmd.extend([image, "-c", bootstrap_sh])
    return cmd


def _run_suite(
    layer: str,
    name: str,
    cmd: list[str],
    *,
    verbose: bool = False,
    stdin_text: Optional[str] = None,
) -> dict:
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(SERVER_DIR),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - t0
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ran = 0
    skipped = 0
    for m in _RAN_RE.finditer(combined):
        ran += int(m.group(1))
    for sm in _SKIP_RE.finditer(combined):
        skipped += int(sm.group(1))
    executed = max(0, ran - skipped)
    ok = proc.returncode == 0
    if verbose or not ok:
        print(f"\n--- [{layer.upper()}] {name} (rc={proc.returncode}, {elapsed:.2f}s) ---")
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print(proc.stderr.rstrip())
    return {
        "layer": layer,
        "name": name,
        "ok": ok,
        "ran": ran,
        "skipped": skipped,
        "executed": executed,
        "elapsed": elapsed,
    }


def _build_plan(
    layer: str,
    *,
    skip_pc_mcp: bool = False,
    skip_anima: bool = False,
) -> list[tuple[str, str, list[str], Optional[str]]]:
    py_host = str(PC_MCP_PYTHON) if PC_MCP_PYTHON.is_file() else sys.executable
    upstream_image = resolve_upstream_image()
    plan: list[tuple[str, str, list[str], Optional[str]]] = []

    if layer in ("unit", "all"):
        if not skip_pc_mcp:
            plan.append(
                (
                    "unit",
                    "Host pc-mcp (L1/L2/L3 security, guardrails, watchdog, vision, sidecar, launcher, common, reqs)",
                    [py_host, "-m", "unittest", "discover", "-s", str(PC_MCP_DIR), "-p", "test_*.py", "-v"],
                    None,
                )
            )
        if not skip_anima:
            plan.append(
                (
                    "unit",
                    "Anima Brain & Obsidian Exporter (Docker isolated, built image + B-1 inbox & rollback)",
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--entrypoint",
                        "python",
                        "-v",
                        f"{ANIMA_DIR / 'tests'}:/app/tests:ro",
                        "-v",
                        f"{ANIMA_DIR / 'scripts'}:/app/scripts:ro",
                        "xiaozhi-anima-brain:local",
                        "-m",
                        "unittest",
                        "/app/tests/test_xiaozhi_service.py",
                        "/app/tests/test_obsidian_exporter.py",
                        "/app/tests/test_diva_anima_mechanisms.py",
                        "-v",
                    ],
                    None,
                )
            )
        plan.append(
            (
                "unit",
                "Container offline suites (single ephemeral docker run --rm, subprocess-isolated)",
                _ephemeral_container_cmd(
                    [
                        "test_mei_emotion_and_prosody.py",
                        "test_batch3_anima_outbox.py",
                        "test_memory_archives.py",
                        "test_batch3_pipeline_vad.py",
                        "test_batch3_console.py",
                        "test_cloud_audio_api.py",
                        "test_diva_borrowed_mechanisms.py",
                    ],
                    image=upstream_image,
                ),
                None,
            )
        )
        if shutil.which("node"):
            plan.append(
                (
                    "unit",
                    "Host Node.js vm console frontend audit (CAS rollback, Dream offline, semantic UI, DOM IDs)",
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os, sys, unittest; "
                            "os.environ['XIAOZHI_REQUIRE_NODE_VM'] = '1'; "
                            f"os.environ['XIAOZHI_APP_DIR'] = {str(APP_DIR)!r}; "
                            f"sys.path.insert(0, {str(TESTS_CONTAINER_DIR)!r}); "
                            "import test_diva_borrowed_mechanisms as m; "
                            "suite = unittest.TestSuite([m.TestDivaBorrowedMechanisms('test_r2_frontend_dom_audit_and_vm_cas_offline_semantic_checks')]); "
                            "res = unittest.TextTestRunner(verbosity=2).run(suite); "
                            "raise SystemExit(0 if res.wasSuccessful() else 1)"
                        ),
                    ],
                    None,
                )
            )

    if layer in ("contract", "all"):
        plan.append(
            (
                "contract",
                "Upstream image contracts (ephemeral docker run --rm: SERVER_VERSION, 21 patches + PATCH_CATALOG sync, zero side-effects)",
                _ephemeral_container_cmd("test_upstream_contracts.py", image=upstream_image),
                None,
            )
        )

    if layer in ("live", "all"):
        live_test_src = (TESTS_CONTAINER_DIR / "test_mei_emotion_and_prosody.py").read_text(encoding="utf-8")
        plan.append(
            (
                "live",
                "Live integration (18000/18003/18080/18787/18900/19880)",
                [
                    "docker",
                    "exec",
                    "-i",
                    "-e",
                    "XIAOZHI_RUN_LIVE_TESTS=1",
                    "xiaozhi-esp32-server",
                    "python3",
                    "-",
                    "TestLiveOnlineIntegration",
                    "-v",
                ],
                live_test_src,
            )
        )
        plan.append(
            (
                "live",
                "Live A-1 least-privilege env isolation (anima-brain has zero PC_MCP_TOKEN)",
                [
                    "docker",
                    "exec",
                    "xiaozhi-anima-brain",
                    "python",
                    "-c",
                    (
                        "import os, unittest\n"
                        "class _T(unittest.TestCase):\n"
                        "    def test_anima_container_has_no_pc_mcp_token(self):\n"
                        "        self.assertNotIn('PC_MCP_TOKEN', os.environ, 'anima-brain MUST NOT receive PC_MCP_TOKEN')\n"
                        "        self.assertTrue(len((os.environ.get('ANIMA_STUDIO_TOKEN') or '').strip()) >= 24, 'anima-brain missing ANIMA_STUDIO_TOKEN')\n"
                        "res = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(_T))\n"
                        "raise SystemExit(0 if res.wasSuccessful() else 1)\n"
                    ),
                ],
                None,
            )
        )

    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="XiaoZhi 3-layer test runner (unit / contract / live / all)")
    parser.add_argument(
        "--layer",
        choices=("unit", "contract", "live", "all"),
        default="all",
        help="Test layer to run (default: all)",
    )
    parser.add_argument("--skip-pc-mcp", action="store_true", help="Skip host Windows pc-mcp unittest suite")
    parser.add_argument("--skip-anima", action="store_true", help="Skip Anima Brain Docker unittest suite")
    parser.add_argument("--sequential", action="store_true", help="Run all suites sequentially instead of parallel offline pool")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print full suite stdout/stderr even on pass")
    args = parser.parse_args(argv)

    plan = _build_plan(args.layer, skip_pc_mcp=args.skip_pc_mcp, skip_anima=args.skip_anima)
    offline_plan = [item for item in plan if item[0] in ("unit", "contract")]
    live_plan = [item for item in plan if item[0] not in ("unit", "contract")]
    results = []

    if len(offline_plan) > 1 and not args.sequential and not args.verbose:
        print(f"==> Running {len(offline_plan)} offline test tracks in parallel ...", flush=True)
        for lyr, name, _, _ in offline_plan:
            print(f"    * [{lyr.upper()}] {name}", flush=True)
        with ThreadPoolExecutor(max_workers=min(4, len(offline_plan))) as pool:
            futs = [
                pool.submit(_run_suite, lyr, name, cmd, verbose=False, stdin_text=stdin_text)
                for lyr, name, cmd, stdin_text in offline_plan
            ]
            for fut in futs:
                res = fut.result()
                status = "PASS" if res["ok"] else "FAIL"
                skip_note = f" (skipped={res['skipped']})" if res["skipped"] else ""
                print(
                    f"    [{res['layer'].upper()}] {status}: {res['executed']} executed{skip_note} in {res['elapsed']:.2f}s — {res['name']}",
                    flush=True,
                )
                results.append(res)
    else:
        for lyr, name, cmd, stdin_text in offline_plan:
            print(f"==> [{lyr.upper()}] {name} ...", flush=True)
            res = _run_suite(lyr, name, cmd, verbose=args.verbose, stdin_text=stdin_text)
            status = "PASS" if res["ok"] else "FAIL"
            skip_note = f" (skipped={res['skipped']})" if res["skipped"] else ""
            print(f"    {status}: {res['executed']} executed{skip_note} in {res['elapsed']:.2f}s", flush=True)
            results.append(res)

    for lyr, name, cmd, stdin_text in live_plan:
        print(f"==> [{lyr.upper()}] {name} ...", flush=True)
        res = _run_suite(lyr, name, cmd, verbose=args.verbose, stdin_text=stdin_text)
        status = "PASS" if res["ok"] else "FAIL"
        skip_note = f" (skipped={res['skipped']})" if res["skipped"] else ""
        print(f"    {status}: {res['executed']} executed{skip_note} in {res['elapsed']:.2f}s", flush=True)
        results.append(res)

    total_exec = sum(r["executed"] for r in results)
    all_ok = all(r["ok"] for r in results)
    print("\n================ Test Summary ================")
    for r in results:
        tag = "PASS" if r["ok"] else "FAIL"
        print(f"  [{r['layer']:8s}] {tag:4s} | {r['executed']:3d} tests | {r['elapsed']:5.2f}s | {r['name']}")
    print(f"TOTAL EXECUTED: {total_exec} | OVERALL: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
