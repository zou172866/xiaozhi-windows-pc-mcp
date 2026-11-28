# Xiaozhi Windows PC MCP（小智本地服务端与电脑操控套件）

[![Clones](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/zou172866/repo-traffic-archive/main/badges/xiaozhi-windows-pc-mcp.json)](https://github.com/zou172866/repo-traffic-archive)

`xiaozhi-windows-pc-mcp` 是运行在 Windows 本地的小智 AI 语音助手服务端与桌面操控套件。它提供语音对话、三层 Windows 电脑操控、Anima Brain 长期记忆、Obsidian 单向导出和本地多情绪语音合成能力。

本项目支持两种接入方式，且两种方式可以同时保留：

- **没有 ESP32 开发板**：直接使用 Windows 电脑的麦克风、音箱、浏览器控制台或桌面客户端进行语音唤醒、语音对话和文字对话。
- **有 ESP32-S3 开发板**：通过局域网将硬件端直连到本机服务端，可与电脑端共用同一套服务端、长期记忆和电脑操控工具。

仓库内还包含可独立安装的桌面操控 Agent Skill `windows-pc-mcp-automation`，支持通过 `npx skills add` 复用到其他 Agent 环境。

---

## 运行环境要求

在 Windows 电脑上运行本项目，需要满足以下条件：

- **操作系统**：Windows 10 或 Windows 11。
- **容器环境**：已安装并启用 WSL2 后端的 Docker Desktop（安装在非 C 盘且未加入系统 `PATH` 时，启停脚本会自动识别常见路径）。
- **宿主机 Python**：Python 3.10 至 3.12（用于运行宿主机 `18900` 电脑操控服务和桌面客户端，需使用正式安装包而非 Windows Store 占位符）。
- **大模型 API Key**：至少准备一个对话模型 API Key，默认支持国内直连的 DeepSeek（`DEEPSEEK_API_KEY`），也可切换 Gemini、GLM 或本地 LM Studio 模型。
- **可选组件**：
  - 使用 DeepSeek DSH 作为 L3 后台 Agent 时，需安装 Node.js。
  - **无需独立显卡**：没有独立显卡时默认使用免显存 `EdgeTTS` 或云端 TTS；配备本机 RTX 显卡时，可选部署本机 `GPT-SoVITS`（`19880`）。

---

## 安装与启动

### 第 1 步：获取代码并检查基础环境

通过 `git clone` 克隆本仓库，或在 GitHub 页面点击 **Code -> Download ZIP** 下载并解压到本地目录。

首次部署前，在 PowerShell 中执行以下三条命令检查基础环境：

```powershell
wsl --status            # 若报拒绝访问，检查安全软件或系统策略；旧版 Win10 异常时先安装新版 WSL MSI 并重启
docker version          # 确认 Docker Desktop 已启动且使用 WSL2 后端
python --version        # 确认版本为 Python 3.10～3.12（若 winget 源异常，可先执行 winget source reset --force）
```

若首次构建 `anima-brain` 镜像时拉取基础镜像或安装依赖较慢，可在仓库根目录的 `.env` 中添加以下加速配置（可选）：

```ini
DOCKER_MIRROR_PREFIX=docker.m.daocloud.io/library/
NPM_REGISTRY=https://registry.npmmirror.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

### 第 2 步：运行初始化向导

双击根目录的 **`一键初始化配置.bat`**（底层调用 `data/init_setup.ps1`），按提示完成 6 步配置：

1. **本机局域网 IP 与 ESP32 白名单**：
   - 脚本会自动探测真实局域网 IPv4（自动排除 Clash/Mihomo `198.18.x.x`、Tailscale、ZeroTier、WireGuard、WSL、Hyper-V 等虚拟网卡），直接回车确认即可。
   - **没有 ESP32 开发板时**，遇到「ESP32 允许接入 MAC 白名单」提示直接回车留空，不会阻断启动。
2. **L3 后台 Agent 引擎**：在 `DeepSeek DSH`、`WorkBuddy`、`Antigravity` 或「暂不启用 L3」中选一项，并指定默认工作目录（日后可在 Web 控制台免重启切换）。
3. **语音引擎方案**：在「轻量均衡（本地 `SherpaASR` + 免显存 `EdgeTTS`）」「纯云端语音 API」「本机 RTX GPU `GPT-SoVITS`」中选一项。
4. **Anima Brain 长期记忆网络**：选择纯国内直连（默认复用 `DEEPSEEK_API_KEY`）或 Google Gemini（需代理与 `GEMINI_API_KEY`）。
5. **Obsidian 单向记忆导出**：可选择自动探测到的本地 Vault、自定义目录或关闭。
6. **宿主机依赖、防火墙与 API Key**：
   - 自动创建哈希锁定的 `pc-mcp\.venv` 环境。
   - 提示放行 Windows 防火墙 TCP `18000 / 18003` 时：**仅在本机使用、不接入局域网 ESP32 可输入 `n` 跳过**；需要局域网连接 ESP32 时输入 `y` 放行。
   - 按提示粘贴 API Key，脚本会写入当前 Windows 用户环境变量，不向配置文件写入明文密钥。

### 第 3 步：启动全部服务

双击根目录的 **`启动或重启服务.bat`**（底层调用 `data/start_all.ps1`）。脚本按以下顺序执行：

1. `[0/4]` 校验 `.env` 与最小权限令牌文件。
2. `[1/4]` 检测并启动 Docker Desktop（WSL2）。
3. `[2/4]` 无黑框启动宿主机 `18900` 电脑操控与 48kHz 音频桥接服务，并启动看门狗 `pc-mcp/pc_mcp_watchdog.py`。
4. `[3/4]` 若启用了本地 GPU 语音则启动 `19880` `GPT-SoVITS` 并预热；未启用时自动跳过。
5. `[4/4]` 启动 `xiaozhi-esp32-server` 与 `xiaozhi-anima-brain` 双容器。主镜像拉取 `ghcr.nju.edu.cn` 失败时自动回退同摘要 `ghcr.io` 镜像；选择本地 `SherpaASR` 且首次缺少模型文件时，容器会自动通过 HTTPS 直连下载轻量模型（约 230 MB）并持久化到 `models/sherpa-onnx-sense-voice/`。

终端提示服务就绪后，即可开始使用。需要停止服务时，双击根目录的 **`关闭小智服务.bat`**。

---

## 开始使用

### 方式 A：没有 ESP32 硬件（纯 Windows 电脑使用）

服务启动后，不需要任何额外硬件，可直接选择以下任意入口开始使用：

#### 1. 网页控制台电脑麦克风（支持语音唤醒待命）

双击 **`打开网页控制台.bat`**，在浏览器打开 `http://localhost:18080`。右侧对话区底部提供电脑麦克风控制栏：

- **电脑麦替代 ESP32（唤醒待命）**：打开该开关后，直接对着电脑喊唤醒词即可自动唤醒并开始对话（默认模板唤醒词为「你好小智」「嘿你好呀」）；说出「退出对话」「退下」「休眠」「拜拜」「再见」等词后，对话自动回到待命状态并整理记忆。
- **开麦自由聊**：无需喊唤醒词，点击后直接连续语音对话。
- **按住说话**：按住按钮说话，松开后立即识别并发送。
- **打断**：点击后立即停止当前语音播报。
- **耳机插话**：默认启用外放防回声（助手播报期间自动闭麦）；佩戴耳机时勾选此项，可在播报期间随时语音插话。

首次开启网页电脑麦时，服务端会自动将本地设备 ID `pc-talk-local` 加入白名单，并将音频输出切换为 Windows 48kHz 电脑音响，无需手动配置鉴权。

待命状态默认使用 `SileroVAD + SherpaASR` 本地识别唤醒词。如需使用专用的低开销流式中文关键词唤醒引擎（`sherpa-onnx-kws`，约 15 MB），可执行以下命令下载模型到 `models/sherpa-onnx-kws/`（不下载也能正常使用唤醒待命）：

```powershell
python scripts/download_kws_model.py
```

#### 2. 独立电脑语音客户端（不打开浏览器）

双击根目录的 **`电脑语音对话.bat`**（底层运行 `python scripts/pc_talk.py --mic`）。

该客户端使用 Python 标准库调用 Windows 原生 WinMM 16kHz 麦克风采集，经本机 `18900` 服务以 48kHz 电脑音箱播报，同时支持在同一窗口内输入文字。内置终端指令：

- `/mic`：开启或关闭电脑麦克风。
- `/duplex`：在外放防回声模式与耳机全双工模式之间切换。
- `/stop`：立即打断当前语音播报。
- `/quit`：结束会话并退出。

命令行额外参数：`--list-mics` 列出可用录音设备，`--mic-device <编号>` 指定麦克风，`--full-duplex` 启动时启用耳机插话，`--keep-audio-target` 保持原播放端不自动切换。

#### 3. 独立电脑打字客户端

双击根目录的 **`电脑对话客户端.bat`**（底层运行 `python scripts/pc_talk.py`）。

该客户端默认不开麦克风，在终端输入文字后通过电脑音响播放语音回复并同步显示字幕。也支持单轮脚本调用：

```powershell
python scripts/pc_talk.py --once "你好，帮我查一下今天的天气"
```

#### 4. 桌角陪伴小窗（可选，替代硬件表情屏幕）

双击根目录的 **`桌角陪伴小窗.bat`**（底层运行 `scripts/desktop_companion.py`）。

小窗常驻在桌面角落，显示当前 7 种情绪头像、对话状态与操控挡位，不会抢占键盘输入焦点。支持拖拽并记忆坐标、双击折叠为胶囊形态、右键触发看屏与快捷入口。若在 `voices/<角色目录>/portraits/` 下放置 `{emotion,default}.{png,webp,jpg}` 头像文件，小窗会自动加载；未放置时自动显示内置 7 情绪矢量头像。

#### 5. QQ 官方机器人双向桥（可选，手机或远程文字交互）

在 `http://localhost:18080` 控制台中启用 QQ 机器人桥接（默认关闭，未启用时不占线程和内存）。

配置官方 `QQ_BOT_APP_ID` 与 `QQ_BOT_APP_SECRET` 并完成 6 位配对码本地审批后，可在 QQ 私聊或授权群聊中收发文字对话、接收定时提醒，并在满足双门禁条件时远程派发 L3 后台任务。

---

### 方式 B：有 ESP32-S3 硬件（局域网直连）

1. 将 ESP32-S3 设备的 OTA 地址配置为 `http://<你的局域网IP>:18003/xiaozhi/ota/` 并重新上电。
2. 打开 `http://localhost:18080`，在顶部「设备与电脑客户端接入白名单」卡片中可以看到被拦截的设备 MAC 地址，点击 **一键放行** 立即生效，无需重启服务（OTA 被拒时也会返回 `{"error":"device_not_allowlisted","device_id":"..."}`）。
3. **区分设备端唤醒词与服务端配置**：
   - **设备端离线唤醒词**（休眠时唤醒连网的词）：固化在 ESP32 Flash `0x800000` 的 `assets` 分区模型中，服务端无法远程修改。出厂常见 `v1.9.4` 固件的 `model` 分区仅 960 KB（固定「你好小智」），需升级至 `v2.6.0`（16 MB 分区表）后再刷入自定义 MultiNet7 `assets.bin`（详见 [.agents/skills/xiaozhi-esp32-ops/SKILL.md](.agents/skills/xiaozhi-esp32-ops/SKILL.md)）。
   - **服务端 `wakeup_words` / `interrupt_words`**（位于 `config/runtime/.config.yaml`）：用于在设备连网后匹配上报文本以播放预设应答语，或触发「听我说」立即截断播报并进入聆听。这两项手改后自动热同步，无需重启容器。

---

### 电脑端与 ESP32 同时保留时的并存规则

在同一台电脑上同时保留电脑端与 ESP32-S3 时，系统按以下规则协同工作：

- **双麦互斥与自动恢复**：服务端默认开启硬件麦克风互斥（`XIAOZHI_PC_MIC_EXCLUSIVE=1`）。只要电脑麦克风处于开启或唤醒待命状态，服务端会自动使同房间内的 ESP32 静默退回待机，防止两边同时唤醒或产生回声；关闭电脑麦开关或退出客户端后，ESP32 立即恢复正常，无需重启服务。若 ESP32 位于其他房间、需要两端同时可用，可在环境变量中设置 `XIAOZHI_PC_MIC_EXCLUSIVE=0`。
- **白名单与长期记忆**：电脑客户端默认使用设备 ID `pc-talk-local`，与 ESP32 的 MAC 地址分开记录，互不覆盖。Anima Brain 默认设置 `isolate_character_by_device: false`，电脑端与 ESP32 默认共用同一份长期记忆与人设；如需让不同设备拥有独立记忆与角色，可在配置中开启 `isolate_character_by_device` 或设置 `device_character_map`。
- **声音输出目标**：电脑客户端或网页电脑麦连接时，默认将声音输出切至 Windows 48kHz 电脑音响（`target="pc"`）。若 ESP32 平时已配置走电脑音响，切回 ESP32 时无需调整；若 ESP32 平时使用机载小喇叭，停用电脑端后在 `http://localhost:18080` 将音频输出切回 ESP32 喇叭即可。

---

## 核心功能

### 1. Windows 三层电脑操控（`pc-mcp/`，`18900` 端口）

操控挡位保存在宿主机 `pc-mcp/state/pc_control_mode.json`，分为「只聊挡（`off`）」「安全挡（`safe`）」「全权操控挡（`full`）」，可在 `18080` 控制台免重启切换。

- **L1 瞬时系统与视觉感知（安全挡可用，不接管鼠标）**：
  - `pc_system_control`：调节音量、静音、切歌、暂停、锁屏和息屏（带防挂死超时保护）。
  - `pc_open_app_or_web`：打开网页或本地软件，使用 64 位 Win32 焦点处理恢复窗口置顶，内置敏感系统工具与卸载器黑名单。
  - `pc_look_at_screen` / `pc_check_system_status`：截取屏幕调用多模态视觉模型（默认 `deepseek-flash`，与 `gemini-3.5-flash-lite` 互备）看屏答疑与查询系统状态。
- **L2 高分屏键鼠操控（需切至全权操控挡）**：
  - 启用 `SetProcessDpiAwarenessContext(-4)`，消除 `125% / 150% / 200%` 缩放下的点击坐标偏移。
  - 采用 UIA 无障碍控件树、Set-of-Mark 红底角标与局部 1:1 放大精修三阶混合定位，点击前比对目标区域哈希以防画面跳变误点。
  - 内置 `pyautogui.FAILSAFE`、全局急停热键 `Ctrl+Alt+Pause/Break`、终端/运行框/资源管理器地址栏拦截，以及脱敏审计日志 `pc-mcp/state/l2_audit.jsonl`。
- **L3 可插拔后台 Agent 与沙箱（需切至全权操控挡）**：
  - 在 `18080` 控制台免重启四选一：DeepSeek DSH（`@deepseek-ai/dsh`）、腾讯 WorkBuddy / CodeBuddy CLI、Google Antigravity Sidecar 或关闭 L3；同一工作目录强制单任务互斥与排队/取消。
  - 宿主机 `pc-mcp/pc_mcp_modules/sandbox.py` 提供原生 Windows `AppContainer` 零网络隔离沙箱、重解析点/硬链接拦截与 Job Object 超时终止，配套后台桌面、Procedure 与任务栏收件箱模块。

### 2. 本地 Web 控制台（`http://localhost:18080`）

Web 控制台仅绑定本机 `127.0.0.1:18080`，启动时生成随机 `X-Console-Token` 与严格 `CSP` 策略。控制台支持免重启热切以下项目：

- 电脑操控挡位、L3 后台引擎、音频输出端（ESP32 喇叭 / Windows 48kHz 电脑音响）；
- 对话模型、语音识别（ASR）与语音合成（TTS）通道、回复字数与讲故事篇幅；
- Anima Brain 亲密度/浪漫尺度、对话记忆存档（日常陪伴 / 酒馆剧情）、Obsidian 导出开关、Outbox 死信重放；
- 投机性 VAD 开关与参数、ASR 同音词纠错表、空闲告别模式、QQ 机器人桥接、定时提醒列表；
- `/api/health` 26 项启动补丁契约状态、LLM 前缀缓存命中率与 TTFT 遥测。

### 3. 长期情感记忆与 Obsidian 单向导出（`anima-brain/`，`18787` 端口）

- **零丢轮收发机制**：`anima-brain` 在 `brain.sqlite3` 中维护持久化 `xiaozhi_inbox`（事务落盘后再返回 HTTP 200，重启自动恢复），主容器侧配备 SQLite Outbox（死信 8 次重试、14 天 TTL，可在 `18080` 一键重放）。
- **中文分层召回**：中文 `episode` 与 `fact` 同步写入 FTS5 索引，结合 120 ms 有界查询向量缓存实现低延迟上下文召回；双击 **`打开Companion Studio控制台.bat`** 可在 `http://localhost:18787` 查看与管理记忆面板。
- **Obsidian 单向原子导出**：按 SQLite 触发器序号检测变更，生成 `总览.md`、`对话/YYYY/MM/YYYY-MM-DD.md` 与 `记忆/` 双链笔记。

### 4. 语音管线、延迟治理与常用内置工具

- **低内存与防抢话**：默认 `SherpaASR`（int8 ONNX 纯内存解码）+ 独立单线程 ASR 执行器 + 空闲冷内存回收，双容器合计常驻约 `480–500 MiB`（`xiaozhi-esp32-server` 约 `415 MiB`、`xiaozhi-anima-brain` 约 `67–84 MiB`）；`SileroVAD` 保留 `1100 ms` 防抢话上限，并内置可热关的投机性句末早判（完整句平均缩短约 `480 ms` 尾延迟）。
- **7 情绪语音路由**：本地 `GPT-SoVITS`、云端流式/HTTP TTS 与 `EdgeTTS` 均支持逐句表情同步；内置免 Key 中央气象台 `nmc.cn` 全国实时与 7 天天气查询。
- **定时提醒、多模型兜底与技能按需加载**：
  - `app/future_tasks.py` 提供 SQLite WAL 持久化与标准 5 段 Cron 调度（最小间隔 5 分钟、上限 50 条、重启防风暴跳过），支持桌面播报、手机推送与 QQ 提醒。
  - `app/llm_fallback_router.py`（主服务首包前安全切备）与 `anima-brain` `FallbackProvider`（仅针对确定性 `429/502/503/504/ConnectError` 切备，不对不确定 `ReadTimeout` 重复计费）提供双端自动兜底。
  - `app/skill_loader.py` 仅向动态 `<turn_context>` 注入技能摘要，保持静态 system prompt 不变，由 `load_skill` 按需加载正文。

### 5. 端口分配表（统一高于 `15000`，避开 Hyper-V 保留段）

| 端口 | 绑定地址 | 归属服务 | 用途 |
| :--- | :--- | :--- | :--- |
| `18000` | `0.0.0.0` | Docker `xiaozhi-esp32-server` | 设备与客户端 WebSocket（MAC 白名单 + HMAC Bearer Token） |
| `18003` | `0.0.0.0` | Docker `xiaozhi-esp32-server` | OTA 鉴权签发与 Vision 视觉接口 |
| `18080` | `127.0.0.1` | Docker `xiaozhi-esp32-server` | Web 主控制台与浏览器电脑麦桥接（`/ws/pc-mic`） |
| `18787` | `127.0.0.1` | Docker `xiaozhi-anima-brain` | Companion Studio、长期记忆引擎与 Obsidian 导出 |
| `18900` | `127.0.0.1` | 宿主机 `pc-mcp\.venv` | Windows 三层操控 MCP 与 48kHz WinMM 播音服务 |
| `19880` | `127.0.0.1` | 宿主机 `gpt-sovits-studio`（可选） | 本机 GPU `GPT-SoVITS` 多情绪语音合成 API |

---

## 进阶配置

### 启用本机 RTX GPU `GPT-SoVITS`（可选，`19880` 端口）

配备 NVIDIA 独立显卡且希望使用本地自定义多情绪音色时，克隆上游 `GPT-SoVITS` 仓库后，可执行随包免编译安装脚本配置环境并注入 `--log_file`、`/prime` 预热与防静音补丁（详见 [gsv-overlay/README.md](gsv-overlay/README.md)）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\gsv-overlay\setup_gsv_windows.ps1
```

### 日常记忆与 SillyTavern 酒馆剧情分档

在 [小智控制台](http://localhost:18080/) 的「对话记忆存档」中选择设备，可在「日常陪伴」与「酒馆剧情」之间切换。结束当前语音连接后重新唤醒，新连接会使用选中的存档，原有日常对话继续保留在原 Anima 角色中。

安装 [「用小智继续」酒馆扩展](integrations/sillytavern/README.md) 后，可从单人聊天导出最近 1 至 3 轮对白，在控制台预览确认并点击「导入并切换到酒馆」。不同聊天分别存档，互不串档。

### 切换本地中文陪伴模型（LM Studio `19340` 端口）

打开 [小智控制台](http://localhost:18080/)，在 **对话模型 -> 对话模式** 中选择 **本地陪伴 · Huihui Qwen3 14B** 或 **DeepSeek 国内直连**，点击右上角 **保存并立即生效**。下一轮对话沿用当前人设和记忆。

本地模式连接宿主机 LM Studio 的 `19340` 端口，使用模型标识 `huihui-qwen3-14b-companion`。导入 Huihui Qwen3 14B Abliterated v2 的 GGUF 后执行（将 `<模型键>` 替换为 `lms ls` 中的对应值）：

```powershell
lms load <模型键> --identifier huihui-qwen3-14b-companion --context-length 16384 --parallel 1
lms server start --port 19340
```

若模型出现替用户接话或念出思考过程，检查 GGUF 的 `tokenizer.chat_template`，在模板首行设置 `{%- set enable_thinking = false %}`。可使用 llama.cpp 的 `gguf-new-metadata 原文件.gguf 副本.gguf --chat-template-file 模板.jinja` 生成副本，参考 [Huihui 官方对话模板](https://huggingface.co/huihui-ai/Huihui-Qwen3-14B-abliterated-v2/blob/3b79629fdd65004d3b9cdf5beb0739e8c7e1becd/chat_template.jinja)。

---

## 目录结构

```text
xiaozhi-server/
├── docker-compose.yml                   # 主服务与非 root Anima Brain 双容器编排
├── docker-compose.override.yml.example  # 开发模式整目录挂载模板（可选，不入库）
├── .github/workflows/ci.yml             # 4-job GitHub Actions CI
├── 一键初始化配置.bat                    # 跨机初始化向导
├── 启动或重启服务.bat / 关闭小智服务.bat  # 无黑框 WMI 启停脚本
├── 打开网页控制台.bat / 查看实时日志.bat  # 18080 Web 控制台与实时日志入口
├── 打开Companion Studio控制台.bat        # 18787 长期记忆面板入口
├── 电脑语音对话.bat                      # 无 ESP32 电脑麦克风 + 48kHz 音箱语音客户端
├── 电脑对话客户端.bat                    # 无 ESP32 电脑打字 + 48kHz 音箱语音客户端
├── 桌角陪伴小窗.bat                      # 桌面悬浮 7 情绪立绘小窗
├── app/                                 # 只读挂入主容器的服务端模块（零导入副作用）
│   ├── boot.py                          # 唯一启动入口（契约补丁 -> 安全门禁 -> 控制台 -> 上游）
│   ├── patches/                         # 26 项契约补丁（鉴权、云音频、电脑麦 PCM、记忆、情绪、定时任务、技能等）
│   ├── console/                         # 18080 Web 控制台（server, shared, routes/*, static/*）
│   └── ...                              # anima_memory_provider, pc_kws_engine, future_tasks, llm_fallback_router, qq_bot_bridge, skill_loader, vad_speculative_router 等
├── anima-brain/                         # 非 root Docker 镜像与记忆引擎源码
├── integrations/sillytavern/            # 「用小智继续」SillyTavern 酒馆剧情交接扩展
├── pc-mcp/                              # 宿主机 Windows 三层操控 MCP 与 48kHz 音频服务（18900）
│   ├── pc_control_mcp.py                # 入口；核心位于 pc_mcp_modules/* 与 common/*
│   ├── pc_mcp_watchdog.py               # 无黑框看门狗（探活、熔断、重启保活 L3 子进程）
│   ├── agy_sidecar_worker.py            # Antigravity 官方 Sidecar 守护进程
│   ├── l3_backends/                     # 可插拔 L3 适配器（dsh / workbuddy / antigravity / none）
│   └── requirements.in / requirements.txt # 依赖声明与 SHA-256 哈希锁文件
├── config/                              # 公开配置模板（.config.example.yaml, runtime_settings.example.json 等）
├── gsv-overlay/                         # 宿主机 GPT-SoVITS 免编译配置与补丁套件（19880）
├── data/                                # 宿主机 PowerShell 运维脚本（start_all.ps1, stop_all.ps1, init_setup.ps1 等）
├── tests/container/                     # 容器离线单测、契约测试与联机集成测试
├── scripts/                             # run_tests.py, pc_talk.py, pc_mic_winmm.py, desktop_companion.py, download_kws_model.py, pack_release.py, generate_patches_doc.py
├── docs/patches.md                      # 自动生成的 26 项补丁契约总表
└── skills/windows-pc-mcp-automation/    # 可独立安装的 Windows 桌面操控 Agent Skill
```

---

## 测试、脱敏打包与延伸文档

### 本地自动化校验

```powershell
python scripts/run_tests.py --layer unit       # 离线单元测试（临时 docker run --rm，不依赖运行中容器）
python scripts/run_tests.py --layer contract   # 上游镜像契约校验（升级镜像必跑）
python scripts/run_tests.py --layer all        # 全链路三层回归（unit + contract + live）
python scripts/generate_patches_doc.py --check # 校验 docs/patches.md 与 PATCH_CATALOG 同步
```

### 打包脱敏分发包（发送压缩包给他人时使用）

不要直接复制本地工作目录，否则会带出 `.env`、`secrets/`、`backups/`、`anima-data/`、`state/`、`config/runtime/`、`voices/` 或本地 `.venv`。使用脱敏打包脚本生成干净压缩包：

```powershell
python scripts/pack_release.py --dry-run   # 执行脱敏、体积上限与脚本编码自检
python scripts/pack_release.py             # 生成 dist/xiaozhi-server-release-YYYYMMDD.zip
```

### 单独安装桌面操控 Agent Skill

```bash
npx skills add zou172866/xiaozhi-windows-pc-mcp
```

### 延伸文档

- 补丁契约总表：[docs/patches.md](docs/patches.md)
- 完整架构、硬件焊接引脚与排障手册：[项目交接与运维手册.md](项目交接与运维手册.md)
- 维护红线与防回归清单：[AGENTS.md](AGENTS.md)

---

## 更新记录

### v2.0.0（2026-11-28）· 定时提醒、多模型兜底、QQ 双向桥、AppContainer 沙箱与无硬件电脑麦升级

- **免 ESP32 电脑麦克风与唤醒待命**：新增 `18080` 网页端「电脑麦替代 ESP32（唤醒待命）」开关、开麦自由聊、按住说话与耳机插话，新增 `电脑语音对话.bat`（WinMM 16kHz 采集）、`桌角陪伴小窗.bat`（桌面 7 情绪立绘）、可选 `Sherpa-ONNX` 流式中文唤醒引擎（`scripts/download_kws_model.py`），以及默认开启的电脑麦与 ESP32 互斥自动恢复机制（`XIAOZHI_PC_MIC_EXCLUSIVE=1`）。
- **原生定时提醒与 5 段 Cron 调度**：新增 [app/future_tasks.py](app/future_tasks.py)（SQLite WAL + 标准库 5 段 Cron、最小间隔 5 分钟、50 条上限、重启防风暴跳过）与 `manage_future_task` 工具，支持桌面播报、手机推送与 QQ 主动提醒。
- **双端多模型自动兜底**：新增 [app/llm_fallback_router.py](app/llm_fallback_router.py)（首包前安全切备、节点冷却、防 SSRF 校验）与 `anima-brain` `FallbackProvider`（仅在 `429/502/503/504/ConnectError` 切备，不对 `ReadTimeout` 重复计费）。
- **QQ 官方机器人双向桥与外部任务通知**：新增 [app/qq_bot_bridge.py](app/qq_bot_bridge.py)（默认关闭、`wss://*.sgroup.qq.com` 白名单、6 位配对码审批、连发攒批防抖与引用块注入隔离、远程 L3 双重门禁）及 `POST /api/qq-bot/notify` 受控通知入口。
- **Windows AppContainer 沙箱与技能按需加载**：新增宿主机 [pc-mcp/pc_mcp_modules/sandbox.py](pc-mcp/pc_mcp_modules/sandbox.py)（零网络 `AppContainer` + 句柄级重解析点/硬链接拦截 + Job Object 超时终止）、后台桌面/Procedure/任务栏收件箱模块，以及 [app/skill_loader.py](app/skill_loader.py)（动态 `<turn_context>` 摘要 + `load_skill` 按需脱敏加载）。
- **补丁契约与隐私隔离**：启动补丁契约扩展至 26 项（同步生成 [docs/patches.md](docs/patches.md)），并新增 `.antigravityignore` 隔离本地隐私与运行态目录。

### 2026-11-23 · 酒馆剧情分档、本地中文陪伴模型与罗小黑多情绪音色

- **SillyTavern 剧情与日常记忆分档**：日常陪伴沿用原 Anima 角色，同一设备可独立切换至酒馆剧情，不同聊天各自独立存档。
- **「用小智继续」酒馆扩展交接**：新增 [integrations/sillytavern/](integrations/sillytavern/README.md) 扩展，支持导出最近 1 至 3 轮选定对白并在 `18080` 控制台导入接续。
- **本地中文陪伴模型与 DeepSeek 切换**：`18080` 控制台新增本地陪伴模型 `Huihui Qwen3 14B`（LM Studio `19340` 端口），切换前校验模型列表可用性。
- **罗小黑本地多情绪音色与启动加固**：新增罗小黑（`GPT-SoVITS v5turbo`）7 情绪配置与唤醒回复；Windows 端将 `ESP32_AUTH_KEY` 迁入当前用户 DPAPI 加密存储，并增加宿主机进程与监听端口归属校验。

---

## 致敬开源项目

本项目基于以下开源项目与社区生态构建（各组件均保留其原始开源协议）：

- **小智硬件与服务端底座**
  - [xiaozhi-esp32 (`78/xiaozhi-esp32`)](https://github.com/78/xiaozhi-esp32)：小智 ESP32 开源 AI 语音硬件与固件生态。
  - [xiaozhi-esp32-server (`xinnan-tech/xiaozhi-esp32-server`)](https://github.com/xinnan-tech/xiaozhi-esp32-server)：私有化服务端架构与上游 Docker 镜像底座。
  - [ESP-SR (`espressif/esp-sr`)](https://github.com/espressif/esp-sr)：乐鑫端侧语音前处理与 MultiNet7 离线中文唤醒/命令词框架。
- **长期情感记忆、酒馆剧情与本地大模型生态**
  - [Anima Brain (`spearsheep/anima-brain`)](https://github.com/spearsheep/anima-brain)：由 Jun Yu Chen（`@spearsheep`）与 `@kayllla` 开源的本地优先长期记忆与情绪连续性引擎（MIT）。
  - [SillyTavern (`SillyTavern/SillyTavern`)](https://github.com/SillyTavern/SillyTavern)：开源 AI 角色扮演前端与扩展体系。
  - [Qwen3 (`QwenLM/Qwen3`)](https://github.com/QwenLM/Qwen3)、[Huihui-Qwen3-14B-abliterated-v2](https://huggingface.co/huihui-ai/Huihui-Qwen3-14B-abliterated-v2) 与 [llama.cpp (`ggml-org/llama.cpp`)](https://github.com/ggml-org/llama.cpp)：本地中文陪伴对话模型权重、对话模板与 GGUF 工具链。
- **语音识别、端点检测与语音合成**
  - [GPT-SoVITS (`RVC-Boss/GPT-SoVITS`)](https://github.com/RVC-Boss/GPT-SoVITS)：少样本语音克隆与本地 GPU 多情绪语音合成引擎。
  - [sherpa-onnx (`k2-fsa/sherpa-onnx`)](https://github.com/k2-fsa/sherpa-onnx) 与 [SenseVoice (`FunAudioLLM/SenseVoice`)](https://github.com/FunAudioLLM/SenseVoice)：本地离线中文语音识别与流式关键词唤醒引擎。
  - [Silero VAD (`snakers4/silero-vad`)](https://github.com/snakers4/silero-vad)：本地语音端点检测模型。
  - [edge-tts (`rany2/edge-tts`)](https://github.com/rany2/edge-tts)：免显存语音合成方案。
- **协议与 Windows 桌面操控基础设施**
  - [Model Context Protocol Python SDK (`modelcontextprotocol/python-sdk`)](https://github.com/modelcontextprotocol/python-sdk)：标准化 MCP 工具接入协议。
  - [PyAutoGUI (`asweigart/pyautogui`)](https://github.com/asweigart/pyautogui) 与 [Python-UIAutomation-for-Windows (`yinkaisheng/Python-UIAutomation-for-Windows`)](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)：Windows 高分屏键鼠操控与 UIA 无障碍控件树定位。
