"""Unified 3-layer test runner for xiaozhi-server (P2-24).

Layers:
  - unit:     Pure offline unit tests (host pc-mcp + anima-brain + container offline tests)
  - contract: Upstream xiaozhi-esp32-server image contract & patch signature tests (run after image upgrades)
  - live:     Live end-to-end integration tests across 18000 / 18003 / 18080 / 18787 / 18900 / 19880
  - all:      Runs unit + contract + live and prints a unified summary table

Usage:
  python scripts/run_tests.py --layer unit
  python scripts/run_tests.py --layer contract
  python scripts/run_tests.py --layer live
  python scripts/run_tests.py --layer all
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
PC_MCP_DIR = SERVER_DIR / "pc-mcp"
PC_MCP_PYTHON = PC_MCP_DIR / ".venv" / "Scripts" / "python.exe"
ANIMA_DIR = SERVER_DIR / "anima-brain"

_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+)s")
_SKIP_RE = re.compile(r"skipped=(\d+)")


def _run_suite(layer: str, name: str, cmd: list[str], *, verbose: bool = False) -> dict:
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(SERVER_DIR),
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
    sm = _SKIP_RE.search(combined)
    if sm:
        skipped = int(sm.group(1))
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


def _build_plan(layer: str) -> list[tuple[str, str, list[str]]]:
    py_host = str(PC_MCP_PYTHON) if PC_MCP_PYTHON.is_file() else sys.executable
    plan: list[tuple[str, str, list[str]]] = []

    if layer in ("unit", "all"):
        plan.append(
            (
                "unit",
                "Host pc-mcp (L1/L2/L3 security, guardrails, watchdog, vision, sidecar, launcher)",
                [py_host, "-m", "unittest", "discover", "-s", str(PC_MCP_DIR), "-p", "test_*.py", "-v"],
            )
        )
        plan.append(
            (
                "unit",
                "Anima Brain & Obsidian Exporter (Docker isolated, incl. B-1 inbox & rollback)",
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
                    "-v",
                    f"{ANIMA_DIR / 'obsidian_exporter.py'}:/app/obsidian_exporter.py:ro",
                    "-v",
                    f"{ANIMA_DIR / 'xiaozhi_anima_service.py'}:/app/xiaozhi_anima_service.py:ro",
                    "-v",
                    f"{ANIMA_DIR / 'src' / 'companion_engine' / 'automation.py'}:/app/src/companion_engine/automation.py:ro",
                    "-v",
                    f"{ANIMA_DIR / 'src' / 'companion_engine' / 'http_server.py'}:/app/src/companion_engine/http_server.py:ro",
                    "xiaozhi-anima-brain:local",
                    "-m",
                    "unittest",
                    "/app/tests/test_xiaozhi_service.py",
                    "/app/tests/test_obsidian_exporter.py",
                    "-v",
                ],
            )
        )
        plan.append(
            (
                "unit",
                "Container offline pipeline (emotion, auth, outbox, recall, farewell, state, settings)",
                [
                    "docker",
                    "exec",
                    "xiaozhi-esp32-server",
                    "python3",
                    "/opt/xiaozhi-esp32-server/app/test_mei_emotion_and_prosody.py",
                ],
            )
        )
        plan.append(
            (
                "unit",
                "Batch 3 Anima outbox dead-lettering/replay & per-device recall (B-5, C-6)",
                [
                    "docker",
                    "exec",
                    "xiaozhi-esp32-server",
                    "python3",
                    "/opt/xiaozhi-esp32-server/app/test_batch3_anima_outbox.py",
                ],
            )
        )
        plan.append(
            (
                "unit",
                "Batch 3 voice pipeline, speculative VAD 34-case suite, idle trim & ASR corrections (C-4..8, F-1, G-1/2/4)",
                [
                    "docker",
                    "exec",
                    "xiaozhi-esp32-server",
                    "python3",
                    "/opt/xiaozhi-esp32-server/app/test_batch3_pipeline_vad.py",
                ],
            )
        )
        plan.append(
            (
                "unit",
                "Batch 3 18080 console hardening, allowlists, redaction & B-5/F-1/G-4 API/UI (D-1..7, H-6)",
                [
                    "docker",
                    "exec",
                    "xiaozhi-esp32-server",
                    "python3",
                    "/opt/xiaozhi-esp32-server/app/test_batch3_console.py",
                ],
            )
        )

    if layer in ("contract", "all"):
        plan.append(
            (
                "contract",
                "Upstream image contracts (SERVER_VERSION, 19+1 patches, anchors, rebind)",
                [
                    "docker",
                    "exec",
                    "xiaozhi-esp32-server",
                    "python3",
                    "/opt/xiaozhi-esp32-server/app/test_upstream_contracts.py",
                ],
            )
        )

    if layer in ("live", "all"):
        plan.append(
            (
                "live",
                "Live integration (18000/18003/18080/18787/18900/19880)",
                [
                    "docker",
                    "exec",
                    "xiaozhi-esp32-server",
                    "python3",
                    "-c",
                    (
                        "import os, sys, unittest; "
                        "os.environ['XIAOZHI_RUN_LIVE_TESTS'] = '1'; "
                        "sys.path.insert(0, '/opt/xiaozhi-esp32-server/app'); "
                        "import test_mei_emotion_and_prosody as m; "
                        "suite = unittest.defaultTestLoader.loadTestsFromTestCase(m.TestLiveOnlineIntegration); "
                        "res = unittest.TextTestRunner(verbosity=2).run(suite); "
                        "sys.exit(0 if res.wasSuccessful() else 1)"
                    ),
                ],
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
    parser.add_argument("-v", "--verbose", action="store_true", help="Print full suite stdout/stderr even on pass")
    args = parser.parse_args(argv)

    plan = _build_plan(args.layer)
    results = []
    for lyr, name, cmd in plan:
        print(f"==> [{lyr.upper()}] {name} ...", flush=True)
        res = _run_suite(lyr, name, cmd, verbose=args.verbose)
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
