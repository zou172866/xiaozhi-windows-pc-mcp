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

    def test_06_pack_release_blocks_huge_zip_without_reading_bytes(self):
        from unittest import mock

        pack = _load_script_module("pack_release_oom", SCRIPTS_DIR / "pack_release.py")
        for archive_name in (
            "gpt-sovits-studio.zip",
            "models_backup.7z",
            "weights.tar.gz",
            "dump.tgz",
            "api_v2.py.orig",
            "model.gguf",
        ):
            self.assertIsNotNone(pack.is_forbidden_release_path(archive_name), f"Must forbid {archive_name}")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            huge_txt = root / "accidental_dump.txt"
            huge_txt.write_bytes(b"tiny")
            orig_stat = Path.stat

            def _fake_stat(self_path, *a, **kw):
                st = orig_stat(self_path, *a, **kw)
                if self_path.name == "accidental_dump.txt":
                    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, 14 * 1024**3, st.st_atime, st.st_mtime, st.st_ctime))
                return st

            with (
                mock.patch.object(Path, "stat", _fake_stat),
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes MUST NOT be called on oversized file")),
            ):
                issues = pack.audit_release_files(root, ["accidental_dump.txt"])
            self.assertEqual(len(issues), 1)
            self.assertIn("[OVERSIZE]", issues[0])

            # Binary-whitelisted suffix (e.g. .pdf / .png) > 16 MiB must ALSO be caught by [OVERSIZE] before zipping
            huge_pdf = root / "huge_manual.pdf"
            huge_pdf.write_bytes(b"%PDF")

            def _fake_stat_pdf(self_path, *a, **kw):
                st = orig_stat(self_path, *a, **kw)
                if self_path.name == "huge_manual.pdf":
                    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, 64 * 1024**2, st.st_atime, st.st_mtime, st.st_ctime))
                return st

            with (
                mock.patch.object(Path, "stat", _fake_stat_pdf),
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes MUST NOT be called on oversized binary")),
            ):
                pdf_issues = pack.audit_release_files(root, ["huge_manual.pdf"])
            self.assertEqual(len(pdf_issues), 1)
            self.assertIn("[OVERSIZE]", pdf_issues[0])

    def test_07_docker_cli_path_resolution_and_empty_dotenv_no_remove_item(self):
        start_txt = (DATA_DIR / "start_all.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn('Remove-Item -Path "Env:$k"', start_txt)
        self.assertIn('SetEnvironmentVariable($k, $null, "Process")', start_txt)
        self.assertIn("Resolve-DockerCliPath", start_txt)
        stop_txt = (DATA_DIR / "stop_all.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Resolve-DockerCliPath", stop_txt)

        with tempfile.TemporaryDirectory() as td:
            fake_root = Path(td) / "CustomDocker"
            fake_bin = fake_root / "resources" / "bin"
            fake_bin.mkdir(parents=True)
            fake_exe = fake_bin / "docker.exe"
            fake_exe.write_bytes(b"MZ")
            ps_hp = str((DATA_DIR / "host_process.ps1").resolve())
            cmd = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                (
                    f". '{ps_hp}'; "
                    "$env:Path = 'C:\\Windows\\System32'; "
                    f"$res = Resolve-DockerCliPath -CandidateRoots @('{fake_root}') -SkipExistingCommand; "
                    "Write-Output \"$res|$env:Path\""
                ),
            ]
            out = subprocess.check_output(cmd, text=True, encoding="utf-8").strip().splitlines()[-1]
            resolved_exe, updated_path = out.split("|", 1)
            self.assertEqual(Path(resolved_exe).resolve(), fake_exe.resolve())
            self.assertTrue(updated_path.lower().startswith(str(fake_bin).lower() + ";"))

    def test_08_gsv_overlay_idempotent_patch_and_pc_talk_ws_protocol(self):
        import base64
        import hashlib
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json
        import socket
        import struct
        import threading

        # 1. Verify gsv-overlay/apply_xiaozhi_gsv_patch.py on synthetic upstream checkout + .venv shim
        overlay_patch = _load_script_module(
            "apply_xiaozhi_gsv_patch_test", SERVER_DIR / "gsv-overlay" / "apply_xiaozhi_gsv_patch.py"
        )
        with tempfile.TemporaryDirectory() as td:
            gsv_root = Path(td)
            (gsv_root / "GPT_SoVITS" / "TTS_infer_pack").mkdir(parents=True)
            (gsv_root / "GPT_SoVITS" / "text" / "g2pw").mkdir(parents=True)
            (gsv_root / ".venv" / "Lib" / "site-packages").mkdir(parents=True)

            (gsv_root / "api_v2.py").write_text(
                "import os, sys, argparse, threading\n"
                "import numpy as np\n"
                "from io import BytesIO\n"
                "from fastapi import FastAPI, Response\n"
                "from fastapi.responses import JSONResponse\n"
                "from pydantic import BaseModel\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('-p', '--port', type=int, default=9880)\n"
                "args = parser.parse_args()\n"
                "tts_pipeline = None\n"
                "APP = FastAPI()\n"
                "class TTS_Request(BaseModel):\n"
                "    text: str = None\n"
                "    parallel_infer: bool = True\n"
                "async def tts_handle(req: dict):\n"
                "    try:\n"
                "        fixed_length_chunk = False\n"
                '        req["fixed_length_chunk"] = fixed_length_chunk\n'
                "        sr, audio_data = next(tts_generator)\n"
                "        audio_data = pack_audio(BytesIO(), audio_data, sr, media_type).getvalue()\n"
                '        return Response(audio_data, media_type=f"audio/{media_type}")\n'
                "    except Exception as e:\n"
                '        return JSONResponse(status_code=400, content={"message": "tts failed", "Exception": str(e)})\n'
                '@APP.get("/set_refer_audio")\n'
                "async def set_refer_aduio():\n"
                "    return {}\n",
                encoding="utf-8",
            )
            (gsv_root / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py").write_text(
                "import os\nimport numpy as np\nimport torch\n"
                "class TTS:\n"
                "    def __init__(self, configs):\n"
                "        self.t2s_model: Text2SemanticLightningModule = None\n"
                "        self.t2s_accel = None\n"
                "    def run(self, inputs: dict):\n"
                "        try:\n"
                "            yield 48000, np.zeros(48000, dtype=np.int16)\n"
                "        except Exception as e:\n"
                "            traceback.print_exc()\n"
                "            yield 16000, np.zeros(int(16000), dtype=np.int16)\n"
                "            raise e\n"
                "    def empty_cache(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            (gsv_root / "GPT_SoVITS" / "text" / "chinese.py").write_text(
                "import jieba_fast\nimport logging\n\njieba_fast.setLogLevel(logging.CRITICAL)\nimport jieba_fast.posseg as psg\n",
                encoding="utf-8",
            )
            (gsv_root / "GPT_SoVITS" / "text" / "tone_sandhi.py").write_text(
                "import jieba_fast as jieba\n",
                encoding="utf-8",
            )
            (gsv_root / "GPT_SoVITS" / "text" / "g2pw" / "onnx_api.py").write_text(
                'import os\nsess = InferenceSession(p, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])\n',
                encoding="utf-8",
            )

            ok_pre, _ = overlay_patch.apply_all_patches(gsv_root, check_only=True)
            self.assertFalse(ok_pre)
            ok_apply, _ = overlay_patch.apply_all_patches(gsv_root, install_shims=True)
            self.assertTrue(ok_apply)
            ok_post, _ = overlay_patch.apply_all_patches(gsv_root, check_only=True)
            self.assertTrue(ok_post, "Second check must report already patched (strictly idempotent)")
            self.assertTrue((gsv_root / ".venv" / "Lib" / "site-packages" / "jieba_fast" / "__init__.py").is_file())

        # 2. End-to-end protocol test of scripts/pc_talk.py (OTA 403 -> auto-allow -> OTA 200 -> WS 101 -> hello -> listen/tts)
        pc_talk = _load_script_module("pc_talk_test", SCRIPTS_DIR / "pc_talk.py")
        allowed_devs: set[str] = set()
        audio_targets: list[str] = []

        # Minimal RFC 6455 test server
        ws_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ws_srv.bind(("127.0.0.1", 0))
        ws_srv.listen(2)
        ws_port = ws_srv.getsockname()[1]

        def _ws_server_worker():
            conn, _ = ws_srv.accept()
            with conn:
                raw_req = b""
                while b"\r\n\r\n" not in raw_req:
                    raw_req += conn.recv(4096)
                hdr_txt = raw_req.decode("latin-1")
                assert "Authorization: Bearer signed-jwt-123" in hdr_txt
                key = ""
                for ln in hdr_txt.split("\r\n"):
                    if ln.lower().startswith("sec-websocket-key:"):
                        key = ln.split(":", 1)[1].strip()
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )

                def _recv_client_json():
                    b0, b1 = conn.recv(2)
                    assert b1 & 0x80, "Client frame must be masked per RFC 6455"
                    ln_len = b1 & 0x7F
                    if ln_len == 126:
                        (ln_len,) = struct.unpack("!H", conn.recv(2))
                    mk = conn.recv(4)
                    enc = b""
                    while len(enc) < ln_len:
                        enc += conn.recv(ln_len - len(enc))
                    dec = bytes(b ^ mk[i & 3] for i, b in enumerate(enc))
                    return json.loads(dec.decode("utf-8"))

                def _send_server_json(obj):
                    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    hdr = bytearray([0x81])
                    if len(data) < 126:
                        hdr.append(len(data))
                    else:
                        hdr.append(126)
                        hdr.extend(struct.pack("!H", len(data)))
                    conn.sendall(bytes(hdr) + data)

                hello_msg = _recv_client_json()
                assert hello_msg["type"] == "hello"
                _send_server_json({"type": "hello", "session_id": "sess-pc-01"})

                listen_msg = _recv_client_json()
                assert listen_msg == {"type": "listen", "state": "detect", "text": "测试电脑对话"}
                _send_server_json({"type": "tts", "state": "sentence_start", "text": "收到啦，电脑音箱正在播报！"})
                _send_server_json({"type": "tts", "state": "stop"})

        ws_thread = threading.Thread(target=_ws_server_worker, daemon=True)
        ws_thread.start()

        class _MockOtaAndConsoleHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                if self.path == "/xiaozhi/ota/":
                    dev = (self.headers.get("Device-Id") or "").lower()
                    if dev not in allowed_devs:
                        out = json.dumps({"success": False, "error": "device_not_allowlisted", "device_id": dev}).encode("utf-8")
                        self.send_response(403)
                    else:
                        out = json.dumps(
                            {"websocket": {"url": f"ws://192.168.1.88:{ws_port}/xiaozhi/v1/", "token": "signed-jwt-123"}}
                        ).encode("utf-8")
                        self.send_response(200)
                elif self.path == "/api/esp32-devices":
                    assert self.headers.get("X-Console-Token") == "secret-tok-99"
                    allowed_devs.add(body["device_id"])
                    out = json.dumps({"ok": True}).encode("utf-8")
                    self.send_response(200)
                elif self.path == "/api/audio-output":
                    audio_targets.append(body["target"])
                    out = json.dumps({"ok": True, "target": "pc", "pc_audio_online": True}).encode("utf-8")
                    self.send_response(200)
                else:
                    out = b"{}"
                    self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockOtaAndConsoleHandler)
        http_port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tok_p = Path(td) / ".console_token"
                tok_p.write_text("secret-tok-99\n", encoding="utf-8")
                sess = pc_talk.negotiate_ota_session(
                    ota_url=f"http://127.0.0.1:{http_port}/xiaozhi/ota/",
                    console_url=f"http://127.0.0.1:{http_port}",
                    device_id="pc-talk-local",
                    client_id="test-cli",
                    token_file=tok_p,
                )
                self.assertTrue(sess["auto_allowed"])
                self.assertEqual(audio_targets, ["pc"])
                self.assertEqual(sess["ws_url"], f"ws://127.0.0.1:{ws_port}/xiaozhi/v1/")
                ws_cli, sid = pc_talk.open_xiaozhi_ws_session(sess)
                try:
                    self.assertEqual(sid, "sess-pc-01")
                    replies = pc_talk.run_single_turn(ws_cli, "测试电脑对话", timeout=5.0)
                    self.assertEqual(replies, ["收到啦，电脑音箱正在播报！"])
                finally:
                    ws_cli.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            ws_srv.close()

    def test_09_pc_talk_interactive_mid_turn_stop_barge_in_and_fragmented_ws(self):
        import base64
        import contextlib
        import hashlib
        import io
        import json
        import queue
        import socket
        import struct
        import threading
        import time

        pc_talk = _load_script_module("pc_talk_interactive_test", SCRIPTS_DIR / "pc_talk.py")
        rx_events: queue.Queue[dict] = queue.Queue()
        line_pipe_q: queue.Queue[str] = queue.Queue()

        class _ScriptedStdin:
            def readline(self) -> str:
                return line_pipe_q.get()

        ws_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ws_srv.bind(("127.0.0.1", 0))
        ws_srv.listen(2)
        ws_port = ws_srv.getsockname()[1]

        def _send_raw_ws_frame(conn: socket.socket, fin: bool, opcode: int, payload: bytes):
            hdr = bytearray([(0x80 if fin else 0x00) | (opcode & 0x0F)])
            n = len(payload)
            if n < 126:
                hdr.append(n)
            elif n < 65536:
                hdr.append(126)
                hdr.extend(struct.pack("!H", n))
            else:
                hdr.append(127)
                hdr.extend(struct.pack("!Q", n))
            conn.sendall(bytes(hdr) + payload)

        def _recv_masked_ws_frame(conn: socket.socket) -> tuple[int, bytes]:
            hdr = b""
            while len(hdr) < 2:
                hdr += conn.recv(2 - len(hdr))
            b0, b1 = hdr[0], hdr[1]
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ext = b""
                while len(ext) < 2:
                    ext += conn.recv(2 - len(ext))
                (ln,) = struct.unpack("!H", ext)
            mk = b""
            while len(mk) < 4:
                mk += conn.recv(4 - len(mk))
            enc = b""
            while len(enc) < ln:
                enc += conn.recv(ln - len(enc))
            return op, bytes(b ^ mk[i & 3] for i, b in enumerate(enc))

        def _ws_worker():
            conn, _ = ws_srv.accept()
            with conn:
                raw_req = b""
                while b"\r\n\r\n" not in raw_req:
                    raw_req += conn.recv(4096)
                key = ""
                for ln in raw_req.decode("latin-1").split("\r\n"):
                    if ln.lower().startswith("sec-websocket-key:"):
                        key = ln.split(":", 1)[1].strip()
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                # 1. Hello handshake
                op, raw = _recv_masked_ws_frame(conn)
                assert op == 0x1 and json.loads(raw.decode("utf-8"))["type"] == "hello"
                _send_raw_ws_frame(conn, True, 0x1, json.dumps({"type": "hello", "session_id": "s-int"}).encode("utf-8"))

                # 2. First turn: send >64KiB fragmented text frame with interleaved Ping + binary Opus, WITHOUT tts/stop!
                op1, raw1 = _recv_masked_ws_frame(conn)
                rx_events.put(json.loads(raw1.decode("utf-8")))
                big_txt = "长故事开头" + ("喵" * 24000)  # >70 KiB UTF-8
                big_payload = json.dumps({"type": "tts", "state": "sentence_start", "text": big_txt}, ensure_ascii=False).encode("utf-8")
                _send_raw_ws_frame(conn, False, 0x1, big_payload[:30000])
                _send_raw_ws_frame(conn, True, 0x9, b"hb")  # Interleaved Ping mid-fragmentation!
                _send_raw_ws_frame(conn, True, 0x0, big_payload[30000:])
                _send_raw_ws_frame(conn, True, 0x2, b"\x00\x01opus-frame")

                # Drain Pong + expect immediate `abort` from `/stop` while turn is STILL active (no tts/stop sent yet!)
                while True:
                    op_next, raw_next = _recv_masked_ws_frame(conn)
                    if op_next == 0xA:  # Pong
                        continue
                    if op_next == 0x8:
                        break
                    msg = json.loads(raw_next.decode("utf-8"))
                    rx_events.put(msg)
                    if msg.get("type") == "abort":
                        _send_raw_ws_frame(conn, True, 0x1, json.dumps({"type": "tts", "state": "stop"}).encode("utf-8"))
                    elif msg.get("type") == "listen" and msg.get("text") == "第二句插话":
                        # Deliberately keep speaking until /quit triggers auto-abort on exit
                        _send_raw_ws_frame(
                            conn, True, 0x1, json.dumps({"type": "tts", "state": "sentence_start", "text": "正在回答第二句"}).encode("utf-8")
                        )

        threading.Thread(target=_ws_worker, daemon=True).start()
        try:
            ws_cli, sid = pc_talk.open_xiaozhi_ws_session(
                {"ws_url": f"ws://127.0.0.1:{ws_port}/xiaozhi/v1/", "ws_token": "tok", "device_id": "pc-talk-local", "client_id": "cli"}
            )
            self.assertEqual(sid, "s-int")
            loop_done = threading.Event()

            def _run_loop():
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        pc_talk.run_interactive_loop(ws_cli, stdin_stream=_ScriptedStdin(), turn_timeout=15.0)
                finally:
                    loop_done.set()

            threading.Thread(target=_run_loop, daemon=True).start()
            line_pipe_q.put("讲个长故事\n")
            ev1 = rx_events.get(timeout=3.0)
            self.assertEqual(ev1, {"type": "listen", "state": "detect", "text": "讲个长故事"})

            # Give _rx_worker a moment to receive sentence_start (still NO tts/stop!), then type /stop mid-utterance
            time.sleep(0.15)
            t_stop = time.monotonic()
            line_pipe_q.put("/stop\n")
            ev_abort = rx_events.get(timeout=1.5)
            elapsed_abort = time.monotonic() - t_stop
            self.assertEqual(ev_abort, {"type": "abort", "reason": "wake_word_detected"})
            self.assertLess(elapsed_abort, 1.2, "Typing /stop mid-utterance must interrupt immediately without waiting for turn_timeout")

            # Send second turn and /quit while speaking -> must auto-send abort before closing
            line_pipe_q.put("第二句插话\n")
            ev2 = rx_events.get(timeout=2.0)
            self.assertEqual(ev2, {"type": "listen", "state": "detect", "text": "第二句插话"})
            time.sleep(0.1)
            line_pipe_q.put("/quit\n")
            ev_quit_abort = rx_events.get(timeout=2.0)
            self.assertEqual(ev_quit_abort, {"type": "abort", "reason": "wake_word_detected"})
            self.assertTrue(loop_done.wait(timeout=2.0))
        finally:
            ws_srv.close()

    def test_10_gsv_overlay_prompt_lru_and_accel_oom_no_thrash(self):
        from collections import OrderedDict

        overlay_patch = _load_script_module(
            "apply_xiaozhi_gsv_patch_lru_test", SERVER_DIR / "gsv-overlay" / "apply_xiaozhi_gsv_patch.py"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wav_a = root / "calm.wav"
            wav_b = root / "happy.wav"
            wav_a.write_bytes(b"RIFF")
            wav_b.write_bytes(b"RIFF")

            # Compile and execute _TTS_PRIME_METHODS_FALLBACK in an isolated mock TTS class to verify zero slot aliasing
            ns: dict = {"os": os, "OrderedDict": OrderedDict, "splits": {"。", "."}}

            class _FakeTorch:
                Tensor = list

                @staticmethod
                def no_grad():
                    return lambda fn: fn

                class cuda:
                    @staticmethod
                    def is_available():
                        return False

            ns["torch"] = _FakeTorch
            exec("class _SlotHarness:\n" + overlay_patch._TTS_PRIME_METHODS_FALLBACK, ns)
            HarnessCls = ns["_SlotHarness"]

            extract_calls: list[tuple[str, str]] = []

            class _MockTTS(HarnessCls):
                def __init__(self):
                    self.configs = type("Cfg", (), {"version": "v5turbo", "vits_weights_path": "s2.pth", "device": "cpu", "is_half": True})()
                    self._prompt_cache_slots = OrderedDict()
                    self.prompt_cache = {
                        "ref_audio_path": None,
                        "prompt_semantic": None,
                        "refer_spec": [],
                        "prompt_text": None,
                        "prompt_lang": None,
                        "phones": None,
                        "bert_features": None,
                        "norm_text": None,
                        "aux_ref_audio_paths": [],
                    }
                    self.text_preprocessor = type(
                        "TP",
                        (),
                        {
                            "segment_and_extract_feature_for_text": lambda _s, pt, pl, _v: (
                                extract_calls.append((pt, pl)) or ([1, 2], f"bert:{pt}", pt)
                            )
                        },
                    )()

                def set_ref_audio(self, p: str):
                    # Upstream TTS.set_ref_audio mutates self.prompt_cache and self.prompt_cache["refer_spec"][0] IN PLACE!
                    self.prompt_cache["ref_audio_path"] = p
                    self.prompt_cache["prompt_semantic"] = [10]
                    if not self.prompt_cache["refer_spec"]:
                        self.prompt_cache["refer_spec"] = [f"spec:{p}"]
                    else:
                        self.prompt_cache["refer_spec"][0] = f"spec:{p}"

            tts = _MockTTS()
            # Prime without trailing period -> normalized with "。" so TTS.run() hits the exact same slot!
            r1 = tts.prime_prompt_cache(str(wav_a), "平静参考句", "zh")
            r2 = tts.prime_prompt_cache(str(wav_b), "开心参考句。", "zh")
            self.assertFalse(r1["cache_hit"])
            self.assertFalse(r2["cache_hit"])
            self.assertEqual(r2["cache_slots"], 2)
            self.assertEqual(len(extract_calls), 2)

            # Hit slot A, then mutate self.set_ref_audio(wav_b) directly as upstream would: slot A in LRU must NOT be corrupted!
            hit_a = tts._sync_or_load_prompt_cache_slot(str(wav_a), [], "平静参考句", "zh", False)
            self.assertTrue(hit_a)
            self.assertEqual(len(extract_calls), 2, "Must hit 8-slot LRU without re-running BERT feature extraction")
            tts.set_ref_audio(str(wav_b))
            hit_a_again = tts._sync_or_load_prompt_cache_slot(str(wav_a), [], "平静参考句。", "zh", False)
            self.assertTrue(hit_a_again)
            self.assertEqual(tts.prompt_cache["ref_audio_path"], str(wav_a))
            self.assertEqual(tts.prompt_cache["refer_spec"], [f"spec:{wav_a}"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
