#!/usr/bin/env python3
"""Generate or verify `docs/patches.md` directly from `app/patches/registry.py` (`PATCH_CATALOG`).

Usage:
  python scripts/generate_patches_doc.py           # write docs/patches.md
  python scripts/generate_patches_doc.py --check   # exit 0 if up to date, exit 1 on drift (CI gate)
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "app" / "patches" / "registry.py"
OUTPUT_PATH = REPO_ROOT / "docs" / "patches.md"


def _load_registry_module():
    spec = importlib.util.spec_from_file_location("_xz_patch_registry_docgen", REGISTRY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load patch registry from {REGISTRY_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _md_escape(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\r", "").replace("\n", " ").strip()


def render_patches_markdown() -> str:
    reg = _load_registry_module()
    catalog: List[Dict[str, Any]] = reg.get_patch_catalog()
    expected_ver: str = getattr(reg, "DEFAULT_EXPECTED_SERVER_VERSION", "0.9.8")
    security_names = sorted(getattr(reg, "SECURITY_PATCH_NAMES", ()))

    sec_count = sum(1 for item in catalog if item.get("category") == "security")
    exp_count = sum(1 for item in catalog if item.get("category") == "experience")

    lines: List[str] = [
        "<!-- AUTO-GENERATED FILE — DO NOT EDIT MANUALLY. -->",
        "<!-- Source of truth: app/patches/registry.py (PATCH_CATALOG) -->",
        "<!-- Regenerate: python scripts/generate_patches_doc.py -->",
        "<!-- Verify in CI: python scripts/generate_patches_doc.py --check -->",
        "",
        "# 启动补丁契约总览（`docs/patches.md`）",
        "",
        "本文档由 `scripts/generate_patches_doc.py` 从 `app/patches/registry.py` 中的 `PATCH_CATALOG` 自动生成，",
        "并与 `tests/container/test_upstream_contracts.py` 双向强校验（运行期实际注册补丁集合必须与 `PATCH_CATALOG` 1:1 匹配）。",
        "",
        "## 概览与启动顺序",
        "",
        f"- **锁定上游镜像版本**：`xiaozhi-esp32-server` `{expected_ver}`（`docker-compose.yml` 锁定 `@sha256` digest）",
        f"- **补丁总数**：`{len(catalog)}` 项（`security` 安全类 `{sec_count}` 项 + `experience` 体验类 `{exp_count}` 项）",
        "- **唯一启动入口**：`app/boot.py`（模块导入零副作用，由 `install_all()` 显式挂载全部补丁 → `patches/contracts.py` 轻量契约自检写入注册表 → `assert_security_boot_gate()` 安全门禁 → 启动 `18080` 控制台与上游 `app.py`）",
        "- **实时自检接口**：`GET http://127.0.0.1:18080/api/health`（返回每个补丁的状态、契约结果、早绑重绑列表与 `contract_self_check` 结果）",
        "",
        "## 失败策略（安全 fail-closed / 体验 fail-soft）",
        "",
        f"- **`security` 类（{', '.join(f'`{n}`' for n in security_names)}）**：任意一项缺失、失败或签名契约不匹配时，`boot.py` 默认拒绝启动（`sys.exit(2)`）；仅当显式设置 `XIAOZHI_ALLOW_INSECURE_BOOT=1` 应急开关时放行，且 `/api/health` 持续返回 HTTP `503` 并在 `18080` 控制台顶栏挂出红色安全告警横幅。",
        "- **`experience` 类**：签名契约不匹配时跳过替换原函数（记录 `skipped_contract`），提示词锚点缺失时逐条软跳过（记录 `partial`）；服务降级回退上游默认行为，`/api/health` 返回 HTTP `200` 且 `degraded` 列表清晰标注降级原因。",
        "",
        "## 补丁明细表",
        "",
        "| # | 补丁名 | 类别 | 实现模块 | 目标符号 | 签名 / 锚点契约 | 失败行为 | 对应测试 |",
        "| :---: | :--- | :---: | :--- | :--- | :--- | :--- | :--- |",
    ]

    for idx, item in enumerate(catalog, start=1):
        name = _md_escape(item.get("name", ""))
        category = _md_escape(item.get("category", ""))
        cat_badge = f"`{category}`"
        module = f"`{_md_escape(item.get('module', ''))}`"
        target = f"`{_md_escape(item.get('target', ''))}`"
        contract = _md_escape(item.get("contract", ""))
        failure = _md_escape(item.get("failure_behavior", ""))
        tests_list = item.get("tests") or []
        tests_cell = "<br>".join(f"`{_md_escape(t)}`" for t in tests_list)
        lines.append(
            f"| {idx} | `{name}` | {cat_badge} | {module} | {target} | {contract} | {failure} | {tests_cell} |"
        )

    lines.extend(
        [
            "",
            "## 升级上游镜像或增删补丁后的必跑命令",
            "",
            "```bash",
            "# 1. 离线跑上游镜像契约测试（临时 docker run --rm，不依赖正在运行的容器）",
            "python scripts/run_tests.py --layer contract",
            "",
            "# 2. 若在 app/patches/registry.py 中增改了 PATCH_CATALOG，重新生成并校验本文档",
            "python scripts/generate_patches_doc.py",
            "python scripts/generate_patches_doc.py --check",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify docs/patches.md from app/patches/registry.py")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that docs/patches.md matches generated output without writing (exits 1 on drift).",
    )
    args = parser.parse_args(argv)

    rendered = render_patches_markdown()
    if args.check:
        if not OUTPUT_PATH.is_file():
            print(f"[ERROR] Missing {OUTPUT_PATH}. Run: python scripts/generate_patches_doc.py", file=sys.stderr)
            return 1
        existing = OUTPUT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        if existing != rendered:
            diff = "".join(
                difflib.unified_diff(
                    existing.splitlines(keepends=True),
                    rendered.splitlines(keepends=True),
                    fromfile=_display_path(OUTPUT_PATH),
                    tofile="generated(docs/patches.md)",
                )
            )
            print("[ERROR] docs/patches.md is out of date with app/patches/registry.py!", file=sys.stderr)
            print(diff[:4000], file=sys.stderr)
            print("Run: python scripts/generate_patches_doc.py", file=sys.stderr)
            return 1
        print(f"[OK] {_display_path(OUTPUT_PATH)} is up to date with PATCH_CATALOG.")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(rendered.encode("utf-8"))
    print(f"[OK] Generated {_display_path(OUTPUT_PATH)} ({len(rendered)} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
