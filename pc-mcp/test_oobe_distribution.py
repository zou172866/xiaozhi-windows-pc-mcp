"""Out-of-the-box distribution tests: PowerShell pre-flight/LAN/Anima/firewall helpers,
Docker mirror & image fallback parsing, and clean release packaging audit.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

PC_MCP_DIR = Path(__file__).resolve().parent
SERVER_DIR = PC_MCP_DIR.parent
DATA_DIR = SERVER_DIR / "data"
SCRIPTS_DIR = SERVER_DIR / "scripts"


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_powershell_json(ps_code: str, *, extra_env: dict[str, str] | None = None) -> object:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_code],
        cwd=str(SERVER_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"PowerShell failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    out = proc.stdout.strip()
    return json.loads(out)


class TestOobeDistributionAndScripts(unittest.TestCase):
    def test_01_lan_ip_detection_excludes_clash_tailscale_zerotier_wireguard_wsl_and_down_nics(self):
        """Both init_setup.ps1 and start_all.ps1 Select-BestLanIPv4 filter VPN/TUN/proxy virtual adapters."""
        for script_rel, lib_var in (
            ("data/init_setup.ps1", "XIAOZHI_INIT_SETUP_LIB_ONLY"),
            ("data/start_all.ps1", "XIAOZHI_START_ALL_LIB_ONLY"),
        ):
            ps_script = str((SERVER_DIR / script_rel).resolve())
            code = f"""
$env:{lib_var} = "1"
. "{ps_script}"
$cands = @(
    [pscustomobject]@{{ IPAddress = "198.18.0.1"; InterfaceAlias = "Meta"; InterfaceDescription = "Wintun Userspace Tunnel"; Status = "Up"; Virtual = $true; InterfaceMetric = 1 }},
    [pscustomobject]@{{ IPAddress = "198.19.0.1"; InterfaceAlias = "Mihomo"; InterfaceDescription = "sing-tun"; Status = "Up"; Virtual = $true; InterfaceMetric = 2 }},
    [pscustomobject]@{{ IPAddress = "100.88.12.34"; InterfaceAlias = "Tailscale"; InterfaceDescription = "Tailscale Tunnel"; Status = "Up"; Virtual = $true; InterfaceMetric = 3 }},
    [pscustomobject]@{{ IPAddress = "10.147.20.5"; InterfaceAlias = "ZeroTier One [abc]"; InterfaceDescription = "ZeroTier Virtual Port"; Status = "Up"; Virtual = $true; InterfaceMetric = 4 }},
    [pscustomobject]@{{ IPAddress = "10.7.0.2"; InterfaceAlias = "wg0"; InterfaceDescription = "WireGuard Tunnel"; Status = "Up"; Virtual = $true; InterfaceMetric = 5 }},
    [pscustomobject]@{{ IPAddress = "172.24.16.1"; InterfaceAlias = "vEthernet (WSL)"; InterfaceDescription = "Hyper-V Virtual Ethernet"; Status = "Up"; Virtual = $true; InterfaceMetric = 6 }},
    [pscustomobject]@{{ IPAddress = "169.254.10.2"; InterfaceAlias = "Ethernet 2"; InterfaceDescription = "Realtek PCIe"; Status = "Up"; Virtual = $false; InterfaceMetric = 10 }},
    [pscustomobject]@{{ IPAddress = "192.168.1.50"; InterfaceAlias = "Ethernet"; InterfaceDescription = "Realtek PCIe"; Status = "Disconnected"; Virtual = $false; InterfaceMetric = 15 }},
    [pscustomobject]@{{ IPAddress = "10.0.0.22"; InterfaceAlias = "USB Ethernet"; InterfaceDescription = "ASIX USB Gigabit"; Status = "Up"; Virtual = $false; InterfaceMetric = 20 }},
    [pscustomobject]@{{ IPAddress = "192.168.31.88"; InterfaceAlias = "WLAN"; InterfaceDescription = "Intel(R) Wi-Fi 6E AX211"; Status = "Up"; Virtual = $false; InterfaceMetric = 35 }}
)
$picked = @(Select-BestLanIPv4 -Candidates $cands)
@{{
    ips = @($picked | ForEach-Object {{ $_.IPAddress }})
    clash_excl = (Test-IsExcludedLanIPv4 -Ip "198.18.0.1")
    ts_excl = (Test-IsExcludedLanIPv4 -Ip "100.100.1.2")
    ts_pub_ok = (-not (Test-IsExcludedLanIPv4 -Ip "192.168.31.88"))
    port_exact = (Test-PortSpecMatches -LocalPort "18000" -TargetPort 18000)
    port_comma = (Test-PortSpecMatches -LocalPort "80, 18000, 18003" -TargetPort 18003)
    port_range = (Test-PortSpecMatches -LocalPort @("80", "18000-18005") -TargetPort 18003)
    port_miss = (Test-PortSpecMatches -LocalPort "8080" -TargetPort 18000)
}} | ConvertTo-Json -Compress
"""
            res = _run_powershell_json(code)
            assert isinstance(res, dict)
            self.assertEqual(res["ips"], ["192.168.31.88", "10.0.0.22"], f"Wrong LAN ranking in {script_rel}: {res}")
            self.assertTrue(res["clash_excl"])
            self.assertTrue(res["ts_excl"])
            self.assertTrue(res["ts_pub_ok"])
            self.assertTrue(res["port_exact"])
            self.assertTrue(res["port_comma"])
            self.assertTrue(res["port_range"])
            self.assertFalse(res["port_miss"])

            # Verify non-admin COM firewall evaluation: ignores Domain-only, app-scoped, loopback-only, UDP, Block, and disabled rules
            fw_code = f"""
$env:{lib_var} = "1"
. "{ps_script}"
$badRules = @(
    [pscustomobject]@{{ Enabled = $true;  Direction = 1; Action = 1; Protocol = 6;   LocalPorts = "18000,18003"; Profiles = 1; ApplicationName = ""; RemoteAddresses = "*" }},
    [pscustomobject]@{{ Enabled = $true;  Direction = 1; Action = 1; Protocol = 6;   LocalPorts = "18000,18003"; Profiles = 2; ApplicationName = "C:\\Program Files\\Chrome\\chrome.exe"; RemoteAddresses = "*" }},
    [pscustomobject]@{{ Enabled = $true;  Direction = 1; Action = 1; Protocol = 6;   LocalPorts = "18000,18003"; Profiles = 2; ApplicationName = ""; RemoteAddresses = "127.0.0.1" }},
    [pscustomobject]@{{ Enabled = $false; Direction = 1; Action = 1; Protocol = 6;   LocalPorts = "18000,18003"; Profiles = 2; ApplicationName = ""; RemoteAddresses = "*" }},
    [pscustomobject]@{{ Enabled = $true;  Direction = 1; Action = 1; Protocol = 17;  LocalPorts = "18000,18003"; Profiles = 2; ApplicationName = ""; RemoteAddresses = "*" }},
    [pscustomobject]@{{ Enabled = $true;  Direction = 2; Action = 1; Protocol = 6;   LocalPorts = "18000,18003"; Profiles = 2; ApplicationName = ""; RemoteAddresses = "*" }},
    [pscustomobject]@{{ Enabled = $true;  Direction = 1; Action = 0; Protocol = 6;   LocalPorts = "18000,18003"; Profiles = 2; ApplicationName = ""; RemoteAddresses = "*" }}
)
$goodRules = @(
    [pscustomobject]@{{ Enabled = $true;  Direction = 1; Action = 1; Protocol = 6;   LocalPorts = "18000, 18003"; Profiles = 2; ApplicationName = ""; RemoteAddresses = "*" }}
)
$evBad = Evaluate-FirewallRulesForPorts -Ports @(18000, 18003) -ActiveProfileMask 2 -FirewallActive $true -Rules $badRules
$evGood = Evaluate-FirewallRulesForPorts -Ports @(18000, 18003) -ActiveProfileMask 2 -FirewallActive $true -Rules $goodRules
$evPub = Evaluate-FirewallRulesForPorts -Ports @(18000, 18003) -ActiveProfileMask 4 -FirewallActive $true -Rules $goodRules
$liveFw = Test-XiaozhiFirewallPorts -Ports @(18000, 18003)
@{{
    bad_missing = @($evBad.MissingPorts)
    good_missing = @($evGood.MissingPorts)
    good_allowed = @($evGood.AllowedPorts)
    pub_missing = @($evPub.MissingPorts)
    pub_flag = $evPub.HasPublicProfile
    live_checked = $liveFw.Checked
}} | ConvertTo-Json -Compress
"""
            fw_res = _run_powershell_json(fw_code)
            assert isinstance(fw_res, dict)
            self.assertEqual(fw_res["bad_missing"], [18000, 18003], f"Bad firewall rules must not match in {script_rel}: {fw_res}")
            self.assertEqual(fw_res["good_missing"], [])
            self.assertEqual(fw_res["good_allowed"], [18000, 18003])
            self.assertEqual(fw_res["pub_missing"], [18000, 18003], "Private rule must not satisfy Public active profile")
            self.assertTrue(fw_res["pub_flag"])
            self.assertTrue(fw_res["live_checked"], f"Non-admin HNetCfg.FwPolicy2 COM check must succeed in {script_rel}")

    def test_02_init_setup_python_validation_rejects_store_stub_and_unsupported_versions(self):
        ps_script = str((DATA_DIR / "init_setup.ps1").resolve())
        code = f"""
$env:XIAOZHI_INIT_SETUP_LIB_ONLY = "1"
. "{ps_script}"
@{{
    stub = (Parse-PythonProbeLine -Line '3.11.9|C:\\Users\\alice\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe')
    py39 = (Parse-PythonProbeLine -Line '3.9.13|C:\\Python39\\python.exe')
    py311 = (Parse-PythonProbeLine -Line '3.11.8|C:\\Python311\\python.exe')
    py312 = (Parse-PythonProbeLine -Line '3.12.7|C:\\Python312\\python.exe')
    py313 = (Parse-PythonProbeLine -Line '3.13.1|C:\\Python313\\python.exe')
}} | ConvertTo-Json -Compress
"""
        res = _run_powershell_json(code)
        assert isinstance(res, dict)
        self.assertIsNone(res["stub"], "Windows Store stub path must be rejected")
        self.assertFalse(res["py39"]["Supported"])
        self.assertTrue(res["py311"]["Supported"])
        self.assertTrue(res["py312"]["Supported"])
        self.assertFalse(res["py313"]["Supported"])

        # Verify Option 1 / Option 2 stale ANIMA_LLM_API_KEY & ANIMA_SUMMARY_API_KEY conflict advisor
        key_code = f"""
$env:XIAOZHI_INIT_SETUP_LIB_ONLY = "1"
. "{ps_script}"
@{{
    opt1_stale_gem = (Get-AnimaKeyConflictAdvice -MemChoice "1" -EffectiveAnimaUrl "https://api.deepseek.com/v1" -DeepSeekKey "sk-ds-new" -UserAnimaKey "AIza-old-gemini" -UserSummaryKey "AIza-old-sum")
    opt1_stale_diff = (Get-AnimaKeyConflictAdvice -MemChoice "1" -EffectiveAnimaUrl "https://api.deepseek.com/v1" -DeepSeekKey "sk-ds-new" -UserAnimaKey "sk-ds-old" -MachineAnimaKey "sk-mach-old" -DotEnvAnimaKey "sk-env-old" -UserSummaryKey "sk-sum-old")
    opt1_orphan_no_ds = (Get-AnimaKeyConflictAdvice -MemChoice "1" -EffectiveAnimaUrl "https://api.deepseek.com/v1" -DeepSeekKey "" -UserAnimaKey "sk-stale-orphan")
    opt1_same = (Get-AnimaKeyConflictAdvice -MemChoice "1" -EffectiveAnimaUrl "https://api.deepseek.com/v1" -DeepSeekKey "  sk-ds-same  " -UserAnimaKey ([char]0xfeff + "sk-ds-same"))
    opt1_custom_url = (Get-AnimaKeyConflictAdvice -MemChoice "1" -EffectiveAnimaUrl "https://api.siliconflow.cn/v1" -DeepSeekKey "sk-ds-new" -UserAnimaKey "sk-sf-key")
    opt1_spoofed_path = (Get-AnimaKeyConflictAdvice -MemChoice "1" -EffectiveAnimaUrl "https://proxy.example.com/deepseek.com/v1" -DeepSeekKey "sk-ds-new" -UserAnimaKey "sk-proxy-key")
    opt2_gem = (Get-AnimaKeyConflictAdvice -MemChoice "2" -EffectiveAnimaUrl "https://generativelanguage.googleapis.com/v1beta/openai" -DeepSeekKey "sk-ds-new" -UserAnimaKey "sk-ds-old" -MachineAnimaKey "sk-mach" -DotEnvAnimaKey "sk-env" -UserSummaryKey "sk-sum-old")
}} | ConvertTo-Json -Compress -Depth 4
"""
        kres = _run_powershell_json(key_code)
        assert isinstance(kres, dict)
        self.assertEqual(kres["opt1_stale_gem"]["UserAction"], "auto_clear")
        self.assertEqual(kres["opt1_stale_gem"]["SummaryUserAction"], "auto_clear")
        self.assertEqual(kres["opt1_stale_diff"]["UserAction"], "prompt_clear")
        self.assertEqual(kres["opt1_stale_diff"]["SummaryUserAction"], "prompt_clear")
        self.assertTrue(kres["opt1_stale_diff"]["WarnMachine"])
        self.assertTrue(kres["opt1_stale_diff"]["WarnDotEnv"])
        self.assertEqual(kres["opt1_orphan_no_ds"]["UserAction"], "prompt_clear")
        self.assertEqual(kres["opt1_orphan_no_ds"]["UserReason"], "orphan_user_anima_key_without_deepseek_key")
        self.assertEqual(kres["opt1_same"]["UserAction"], "none")
        self.assertFalse(kres["opt1_same"]["WarnMachine"])
        self.assertEqual(kres["opt1_custom_url"]["UserAction"], "none")
        self.assertFalse(kres["opt1_custom_url"]["IsDeepSeekUrl"])
        self.assertEqual(kres["opt1_spoofed_path"]["UserAction"], "none")
        self.assertFalse(kres["opt1_spoofed_path"]["IsDeepSeekUrl"])
        self.assertEqual(kres["opt2_gem"]["UserAction"], "auto_clear")
        self.assertEqual(kres["opt2_gem"]["SummaryUserAction"], "auto_clear")
        self.assertTrue(kres["opt2_gem"]["WarnMachine"])
        self.assertTrue(kres["opt2_gem"]["WarnDotEnv"])

    def test_03_start_all_anima_key_fallback_and_image_resolution(self):
        ps_script = str((DATA_DIR / "start_all.ps1").resolve())
        compose_path = str((SERVER_DIR / "docker-compose.yml").resolve())
        code = f"""
$env:XIAOZHI_START_ALL_LIB_ONLY = "1"
. "{ps_script}"
$img = Get-ComposeXiaozhiImageInfo -ComposePath "{compose_path}"
$r_ds_only = Resolve-AnimaEnvFallbacks -EnvMap @{{ DEEPSEEK_API_KEY = "sk-ds-1"; GEMINI_API_KEY = "" }}
$r_gem_url_both = Resolve-AnimaEnvFallbacks -EnvMap @{{
    DEEPSEEK_API_KEY = "sk-ds-1";
    GEMINI_API_KEY = "AIza-gem-2";
    ANIMA_LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai";
    ANIMA_EMBEDDING_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai";
}}
$r_gem_only = Resolve-AnimaEnvFallbacks -EnvMap @{{ DEEPSEEK_API_KEY = ""; GEMINI_API_KEY = "AIza-gem-3" }}
$r_explicit = Resolve-AnimaEnvFallbacks -EnvMap @{{
    DEEPSEEK_API_KEY = "sk-ds-1";
    ANIMA_LLM_API_KEY = "sk-custom-9";
}}
$r_spoof = Resolve-AnimaEnvFallbacks -EnvMap @{{
    DEEPSEEK_API_KEY = "sk-ds-1";
    GEMINI_API_KEY = "AIza-gem-2";
    ANIMA_LLM_BASE_URL = "https://proxy.example.com/googleapis.com/v1";
    ANIMA_EMBEDDING_BASE_URL = "https://proxy.example.com/deepseek.com/v1";
}}
@{{
    primary = $img.Primary
    fallback = $img.Fallback
    ds_only_key = $r_ds_only["ANIMA_LLM_API_KEY"]
    ds_only_sum = $r_ds_only["ANIMA_SUMMARY_API_KEY"]
    gem_url_key = $r_gem_url_both["ANIMA_LLM_API_KEY"]
    gem_url_emb = $r_gem_url_both["ANIMA_EMBEDDING_API_KEY"]
    gem_only_url = $r_gem_only["ANIMA_LLM_BASE_URL"]
    gem_only_key = $r_gem_only["ANIMA_LLM_API_KEY"]
    explicit_key = $r_explicit["ANIMA_LLM_API_KEY"]
    spoof_llm_key = if ($r_spoof.ContainsKey("ANIMA_LLM_API_KEY")) {{ $r_spoof["ANIMA_LLM_API_KEY"] }} else {{ "" }}
    spoof_emb_key = if ($r_spoof.ContainsKey("ANIMA_EMBEDDING_API_KEY")) {{ $r_spoof["ANIMA_EMBEDDING_API_KEY"] }} else {{ "" }}
    mirror_noslash = (Normalize-DockerMirrorPrefix -Prefix "docker.m.daocloud.io/library")
    mirror_slash = (Normalize-DockerMirrorPrefix -Prefix "docker.m.daocloud.io/library/")
    mirror_empty = (Normalize-DockerMirrorPrefix -Prefix "   ")
}} | ConvertTo-Json -Compress
"""
        res = _run_powershell_json(code)
        assert isinstance(res, dict)
        self.assertTrue(res["primary"].startswith("ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:"))
        self.assertIn("@sha256:", res["primary"])
        self.assertEqual(res["fallback"], res["primary"].replace("ghcr.nju.edu.cn/", "ghcr.io/"))
        self.assertEqual(res["ds_only_key"], "sk-ds-1")
        self.assertEqual(res["ds_only_sum"], "sk-ds-1")
        self.assertEqual(res["gem_url_key"], "AIza-gem-2")
        self.assertEqual(res["gem_url_emb"], "AIza-gem-2")
        self.assertIn("googleapis.com", res["gem_only_url"])
        self.assertEqual(res["gem_only_key"], "AIza-gem-3")
        self.assertEqual(res["explicit_key"], "sk-custom-9")
        self.assertEqual(res["spoof_llm_key"], "", "Spoofed path URL must never receive GEMINI_API_KEY fallback")
        self.assertEqual(res["spoof_emb_key"], "", "Spoofed path URL must never receive DEEPSEEK_API_KEY fallback")
        self.assertEqual(res["mirror_noslash"], "docker.m.daocloud.io/library/")
        self.assertEqual(res["mirror_slash"], "docker.m.daocloud.io/library/")
        self.assertEqual(res["mirror_empty"], "")

    def test_04_run_tests_resolve_upstream_image_and_dockerfile_mirror_args(self):
        run_tests = _load_script_module("run_tests_oobe", SCRIPTS_DIR / "run_tests.py")
        prev_override = os.environ.pop("XIAOZHI_TEST_IMAGE", None)
        try:
            img = run_tests.resolve_upstream_image()
            self.assertTrue(img.startswith("ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:"))
            self.assertNotIn("${", img)
            self.assertNotIn("}", img)
            self.assertIn("@sha256:", img)
        finally:
            if prev_override is not None:
                os.environ["XIAOZHI_TEST_IMAGE"] = prev_override

        df_txt = (SERVER_DIR / "anima-brain" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG DOCKER_MIRROR_PREFIX=", df_txt)
        self.assertIn("ARG NPM_REGISTRY=", df_txt)
        self.assertIn("ARG PIP_INDEX_URL=", df_txt)
        self.assertIn("FROM ${DOCKER_MIRROR_PREFIX}node:22-slim@sha256:", df_txt)
        self.assertIn("FROM ${DOCKER_MIRROR_PREFIX}python:3.12-slim@sha256:", df_txt)

        start_txt = (DATA_DIR / "start_all.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Get-Command docker", start_txt)
        self.assertIn("docker compose version", start_txt)
        self.assertIn("if ($serverReady)", start_txt)
        self.assertNotIn("taskkill", start_txt.lower())
        self.assertNotIn("E:\\Docker\\Docker Desktop.exe", start_txt)
        self.assertNotIn("D:\\Docker\\Docker Desktop.exe", start_txt)

    def test_05_pack_release_forbids_private_files_and_audits_secrets_and_encodings(self):
        pack = _load_script_module("pack_release_oobe", SCRIPTS_DIR / "pack_release.py")

        for bad_path in (
            ".env",
            "./.env",
            ".env.local",
            ".env.prod",
            "db.sqlite3",
            "backup.bak",
            "private.pem",
            "data/last_som_grounding.png",
            ".git/config",
            "secrets/anima_tokens.env",
            "backups/nvs_original_backup.bin",
            "anima-data/brain.sqlite3",
            "state/chat_history.json",
            "config/runtime/.config.yaml",
            "pc-mcp/state/pc_control_mode.json",
            "pc-mcp/.venv/Scripts/python.exe",
            "models/sherpa-onnx-sense-voice/model.int8.onnx",
            "voices/raiden_mei/mei_default_ref.wav",
            "logs/pc_mcp_18900.log",
            "docker-compose.override.yml",
            "anima-brain/src/companion_engine/static/voice-player.html",
        ):
            self.assertIsNotNone(pack.is_forbidden_release_path(bad_path), f"Must forbid {bad_path}")

        for good_path in (
            "docker-compose.yml",
            ".env.example",
            "app/boot.py",
            "config/.config.example.yaml",
            "data/init_setup.ps1",
            "data/start_all.ps1",
            "pc-mcp/requirements.txt",
            "scripts/pack_release.py",
        ):
            self.assertIsNone(pack.is_forbidden_release_path(good_path), f"Must allow {good_path}")

        # Audit catches plaintext secrets, personal paths/weights, missing BOM, bare LF, and non-ASCII .bat
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "leak.txt").write_text("API_KEY=sk-1234567890abcdefghijklmno\n", encoding="utf-8")
            (root / "personal.txt").write_text(
                "home=C:\\Users\\RealOwner\\secret\nweight=芽衣-e10.ckpt\n",
                encoding="utf-8",
            )
            (root / "bad.ps1").write_bytes(b"Write-Host 'hi'\n")
            (root / "bad.bat").write_bytes("echo 中文\r\n".encode("utf-8"))
            (root / "good.ps1").write_bytes(b"\xef\xbb\xbfWrite-Host 'ok'\r\n")
            (root / "good.bat").write_bytes(b"@echo off\r\necho ok\r\n")

            issues = pack.audit_release_files(
                root, ["leak.txt", "personal.txt", "bad.ps1", "bad.bat", "good.ps1", "good.bat"]
            )
            joined = "\n".join(issues)
            self.assertIn("[SECRET] leak.txt", joined)
            self.assertIn("[PERSONAL] personal.txt", joined)
            self.assertIn("UTF-8 BOM", joined)
            self.assertIn("CRLF", joined)
            self.assertIn("pure ASCII", joined)
            self.assertNotIn("good.ps1", joined)
            self.assertNotIn("good.bat", joined)

        # Real repository audit + zip packaging into a temp file
        files, _excluded = pack.collect_release_files(SERVER_DIR, tracked_only=False)
        self.assertGreater(len(files), 50)
        issues_real = pack.audit_release_files(SERVER_DIR, files)
        self.assertEqual(issues_real, [], f"Repo release audit failed: {issues_real}")

        with tempfile.TemporaryDirectory() as td_out:
            out_zip = Path(td_out) / "clean_release.zip"
            count, _ = pack.build_release_zip(SERVER_DIR, files, out_zip)
            self.assertEqual(count, len(files))
            with zipfile.ZipFile(out_zip, "r") as zf:
                names = zf.namelist()
            self.assertIn("xiaozhi-server/docker-compose.yml", names)
            self.assertIn("xiaozhi-server/data/init_setup.ps1", names)
            for name in names:
                rel_inner = name.removeprefix("xiaozhi-server/")
                self.assertIsNone(pack.is_forbidden_release_path(rel_inner), f"Leaked into zip: {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
