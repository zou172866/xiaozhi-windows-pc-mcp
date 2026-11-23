# Xiaozhi ESP32 · Windows 三层电脑操控与长期记忆本地服务端

让 **ESP32-S3 小智语音助手** 直连你的本地 Windows 电脑，提供 **高精度 GUI 视觉点击操控 + 可插拔后台 Agent（DSH / WorkBuddy / Antigravity）异步派单 + Anima Brain 长期情感记忆与 Obsidian 单向同步 + 本地 GPU `GPT-SoVITS` 多情绪定制音色** 的一站式私有化方案。

同时内置可独立安装的 Agent Skill `windows-pc-mcp-automation`，支持通过 `npx skills add` 复用到任意 Agent 环境。

---

## 更新说明

### 2026-11-23 · SillyTavern 剧情与日常记忆分档

**开发进度**　已完成本地实现和自动化验证，代码待合入 `main`。

- **日常与剧情分开记忆。** 现有日常记忆沿用原存档。同一台 ESP32 可以切换到酒馆剧情，同一角色的不同酒馆聊天各自保存进度。
- **把酒馆里的一段对话带到音箱。** 「用小智继续」扩展可以从单人聊天导出最近 1 至 3 轮选定的对白。在小智控制台选择设备、预览文件并确认导入，结束当前语音连接后重新唤醒，就能用小智现有的人设和音色接着聊。
- **切换时，旧对话写回原存档。** 即使上一轮在切换后才完成后台保存，也会回到当时的存档。重复导入同一份对白只记一次。

验证覆盖了剧情隔离、跨连接延迟写入、重复导入和浏览器文件交接。项目三层回归通过 281 项，Anima 镜像内测试通过 263 项。实体 ESP32 语音接续待实机验证。

## 核心特性

### 1. Windows 三层电脑操控架构（`pc-mcp/`）
- **L1 瞬时系统与视觉感知（日常/游戏安全挡可用，不抢鼠标）**
  - `pc_system_control`：音量调节、静音、切歌/暂停、锁屏、息屏（防挂死超时保护）；
  - `pc_open_app_or_web`：64 位 Win32 焦点锁破除（`AttachThreadInput` + `HWND_TOPMOST`），打开网页或本地软件自动恢复置顶，内置敏感系统工具/卸载器黑名单与目录索引缓存；
  - `pc_look_at_screen` / `pc_check_system_status`：基于 `deepseek-flash` 原生多模态视觉（~1.5s 国内极速识图，与 `gemini-3.5-flash-lite` 互备）看屏答疑与查系统状态。
- **L2 高分屏 1:1 物理像素键鼠操控（需在 `18080` 控制台切至【全权操控挡】）**
  - `SetProcessDpiAwarenessContext(-4)` 消除 `125% / 150% / 200%` 缩放错位；
  - **UIA 无障碍树 + Set-of-Mark 红底角标 + 局部 1:1 放大二次精修**三阶混合定位，点击前核对目标区域哈希防画面跳变误点；
  - 内置 `pyautogui.FAILSAFE`、全局急停热键 `Ctrl+Alt+Pause/Break`、高危热键与终端/运行框/地址栏拦截，以及脱敏审计日志 `l2_audit.jsonl`。
- **L3 可插拔后台 Agent 调度（`pc-mcp/l3_backends/`，18080 免重启四选一）**
  - **DeepSeek DSH（`@deepseek-ai/dsh`，国内直连）**、**腾讯 WorkBuddy / CodeBuddy CLI（国内直连）**、**Google Antigravity（官方 Sidecar 桥接）** 或 **关闭 L3**；同工作目录单任务互斥与排队/取消。

### 2. 本地 Web 控制台（`http://localhost:18080`）
- 免重启热切 **安全挡位**、**L3 后台引擎**、**ESP32 喇叭 / Windows 48kHz 电脑音响直通**、**回复字数与讲故事篇幅**、**Anima Brain 亲密度/浪漫尺度**、**投机 VAD 开关与参数**、**ASR 同音词纠错表**、**空闲告别模式**、**Obsidian 同步开关**与 **LLM / TTS 模型**；
- 内置 `X-Console-Token` 与严格 `CSP` 防护、云端 ASR/TTS SSRF 门禁与空闲热切、`/api/health` 21 项补丁契约与启动自检状态面板、LLM 前缀缓存命中率与 TTFT 遥测。

### 3. 工程化、稳定性与跨电脑开箱即用
- **纯国内免梯子 & 无独显友好**：双击 `一键初始化配置.bat` 自动探测局域网 IP、配置 `.env` 占位符（密钥零明文落盘），无独显自动跳过 `19880` GPU 引擎走免显存 `EdgeTTS`；
- **高位端口（`>15000`）**：`18000`（WS）、`18003`（OTA/Vision）、`18080`（控制台）、`18787`（Anima Brain）、`18900`（PC MCP + 48kHz 音频）、`19880`（可选 GPU `GPT-SoVITS`），彻底避开 Windows Hyper-V 保留段冲突；
- **低延迟与防抢话**：`SileroVAD` `1100ms` 防抢话上限 + 可热关投机性句末早判（实测 34 条句中换气零误截断、完整句平均省 `~0.48s` 尾延迟）+ ESP32-S3 离线唤醒/打断词；
- **轻量内存与免 Key 天气**：默认 `SherpaASR`（int8 ONNX）+ 异步独立 ASR 线程 + 空闲冷内存回收，实测双容器合计常驻 `~480–500 MiB`（`xiaozhi-esp32-server ~415 MiB` + `xiaozhi-anima-brain ~67–84 MiB`，较 PyTorch `FunASR` `3.54 GB` 压降约 88%）；内置中央气象台 `nmc.cn` 免 Key 全国实时与 7 天天气；
- **可复现构建与自动化 CI**：`anima-brain` 锁定双阶段镜像 digest、Py3.12 依赖约束与非 root `USER app`，宿主机 `pc-mcp` 使用 `requirements.in` + SHA-256 哈希锁依赖，自带 GitHub Actions CI（含 `ruff` 门禁、编码门禁与 257 项 Anima `pytest` 全量回归）及三层 250 项自动化测试套件。

---

## 目录结构

```text
xiaozhi-server/
├── docker-compose.yml                   # 小智主服务 + 非 root Anima Brain 双容器编排
├── docker-compose.override.yml.example  # 开发模式整目录挂载模板（可选，不入库）
├── .github/workflows/ci.yml             # 4-job GitHub Actions CI（Windows pc-mcp / 容器单测契约 / Anima 构建 / 文档校验）
├── 一键初始化配置.bat                    # 跨机开箱向导（纯 ASCII + CRLF）
├── 启动或重启服务.bat / 关闭小智服务.bat  # 无黑框 WMI 启停入口
├── 打开网页控制台.bat / 查看实时日志.bat  # 18080 控制台与日志快捷入口
├── app/                                 # 只读挂入主容器的模块化服务端代码（零导入副作用）
│   ├── boot.py                          # 唯一启动入口（install_all -> 契约自检 -> 安全门禁 -> 控制台 -> 上游）
│   ├── patches/                         # 21 项契约补丁（registry, contracts, auth, cloud_audio, memory_hooks, asr, pc_audio, reply_length, weather, emotion/*）
│   ├── console/                         # 18080 Web 控制台（server, shared, routes/*, static/{index.html,app.css,app.js}）
│   └── ...                              # anima_memory_provider, vad_speculative_router, turn_context_router, xz_common 等
├── anima-brain/                         # 可复现非 root Docker 镜像（锁定 digest + Py3.12 constraints + 构建期 UI 打包）
├── pc-mcp/                              # 宿主机 Windows 三层操控 MCP (18900)
│   ├── pc_control_mcp.py                # 薄入口；核心拆分为 pc_mcp_modules/* 与 common/*
│   ├── pc_mcp_watchdog.py               # 无黑框看门狗（探活、熔断、重启保活 L3 子进程）
│   ├── agy_sidecar_worker.py            # Antigravity 官方 Sidecar 守护进程
│   ├── l3_backends/                     # 可插拔 L3 适配器（dsh / workbuddy / antigravity / none）
│   └── requirements.in / requirements.txt # 直接依赖声明 + SHA-256 哈希锁文件
├── config/                              # 公开配置模板（.config.example.yaml, runtime_settings.example.json 等）
├── data/                                # 宿主机 PowerShell 运维脚本（start_all.ps1, stop_all.ps1, init_setup.ps1）
├── tests/container/                     # 容器离线单测、契约测试与联机集成测试（不进入生产挂载）
├── scripts/                             # run_tests.py（三层测试运行器）与 generate_patches_doc.py（补丁文档生成校验）
├── docs/patches.md                      # 自动生成的 21 项补丁契约总表
└── skills/windows-pc-mcp-automation/    # 可独立安装的 Windows 桌面操控 Agent Skill
```

---

## 快速上手（新 Windows 电脑开箱即用）

1. **双击运行 `一键初始化配置.bat`**：
   - 自动预检 Docker Desktop、宿主机 Python 3.10–3.12（排除 Windows Store 占位符）、Node.js（使用 DSH 时）与 Windows 防火墙 TCP `18000` / `18003`；
   - 自动探测真实局域网 IPv4（过滤 Clash/Mihomo `198.18.x.x`、Tailscale `100.64–127.x.x`、ZeroTier、WireGuard、WSL、Hyper-V 等虚拟网卡）；
   - 选择 L3 引擎（`DSH / WorkBuddy / Antigravity / 关闭`）与语音引擎（免显存 `EdgeTTS` / 云端低配 API / 本地 GPU `GPT-SoVITS`）、创建 `pc-mcp\.venv` 哈希锁环境并安全写入系统环境变量（密钥零明文落盘；纯国内模式下 Anima Brain 默认直连 DeepSeek 并自动复用 `DEEPSEEK_API_KEY`）。
2. **双击 `启动或重启服务.bat`**：
   - 上游主镜像拉取 `ghcr.nju.edu.cn` 失败时自动回退同摘要 `ghcr.io` 镜像；首次缺少本地 SherpaASR 模型时自动通过轻量 HTTPS 直连下载（无 PyTorch 内存暴涨），也可在 `18080` 控制台切云端 ASR。
3. **ESP32 首次一键放行**：将 ESP32 的 OTA 地址指向 `http://<你的局域网IP>:18003/xiaozhi/ota/` 并上电，打开 http://localhost:18080 顶部「ESP32 设备接入白名单」卡片即可看到被拦截的设备 MAC，点击 **「一键放行」** 免重启立即生效。
4. **（可选）打包干净分发版**：严禁直接复制本地工作目录（避免泄露 `.env`、`secrets/`、`backups/`、`anima-data/`、`state/`、`config/runtime/` 或失效的 `pc-mcp/.venv/`），请运行：
   ```powershell
   python scripts/pack_release.py --dry-run   # 脱敏与脚本编码自检
   python scripts/pack_release.py             # 生成干净分发包 dist/xiaozhi-server-release-YYYYMMDD.zip
   ```

### 本地自检与延伸文档

```powershell
python scripts/run_tests.py --layer unit       # 离线单元测试（含临时 docker run --rm，不依赖运行中容器）
python scripts/run_tests.py --layer contract   # 上游镜像契约校验（升级镜像必跑）
python scripts/run_tests.py --layer all        # 全链路三层回归（235 项）
python scripts/generate_patches_doc.py --check # 校验 docs/patches.md 与代码同步
```

- 补丁契约明细：[docs/patches.md](docs/patches.md)
- 完整架构、硬件引脚与运维手册：[项目交接与运维手册.md](项目交接与运维手册.md)
- 维护红线与防回归清单：[AGENTS.md](AGENTS.md)

### 单独安装桌面操控 Agent Skill

```bash
npx skills add zou172866/xiaozhi-windows-pc-mcp
```
