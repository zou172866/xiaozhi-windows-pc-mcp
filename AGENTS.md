# 小智 ESP32 本地服务端与三层电脑操控规范 (`xiaozhi-server/AGENTS.md`)

> 本文件仅保留**硬性红线、核心防回归清单与必跑验证命令**。详细架构、端口说明、硬件引脚与排障指南见 [项目交接与运维手册.md](项目交接与运维手册.md)；全部 21 项启动补丁契约见自动生成的 [docs/patches.md](docs/patches.md)。

---

## 1. 硬性红线（严禁触碰）

- **高位端口铁律（`>15000`）**：严禁回退至 `15000` 以下端口（规避 Windows Hyper-V 动态保留段）：`18000`（WS）、`18003`（OTA/Vision）、`18080`（Web 控制台）、`18787`（Anima Brain）、`18900`（宿主机 PC MCP + 48kHz 音频桥接）、`19880`（宿主机可选 GPU `GPT-SoVITS`）。
- **无黑框启动与精准单进程停止**：宿主机 `18900` / `19880` 必须经 WMI `Win32_Process.Create`（`ShowWindow=0`）挂载于交互桌面 `WinSta0\Default`；停止 `18900` 只允许按 PID 执行 `Stop-Process -Id <PID> -Force`，严禁 `taskkill /T` 误杀用户浏览器。
- **脚本编码铁律**：所有 `.bat` 必须为**纯 ASCII + CRLF**；所有 `.ps1` 必须为 **UTF-8 BOM + CRLF**。
- **容器权限边界与构建纪律**：
  - `docker-compose.yml` 按最小权限挂载：`app/`、`config/`、`voices/`、`.mcp_server_settings.json` 只读入主容器，`config/runtime/` 挂 `/opt/xiaozhi-esp32-server/data`，`state/` 挂 `/opt/xiaozhi-esp32-server/state`，`anima-data/` 仅挂 `anima-brain`；`secrets/`、`backups/`、`logs/`、`models/gsv_weights/` 永不挂入容器；`anima-brain` 严禁接触 `PC_MCP_TOKEN`。
  - `anima-brain` 以非 root `USER app`（uid/gid `1000`）运行，禁止在 `docker-compose.yml` 中新增源码单文件 `:ro` 挂载（防 Windows 原子保存 inode 漂移与镜像版本错位）；改动 `anima-brain/` 后通过 `docker compose build anima-brain` 重建镜像，开发调试使用未跟踪的 `docker-compose.override.yml`（模板见 `docker-compose.override.yml.example`）整目录挂载。
  - `anima-brain/src/companion_engine/static/{memory-panel,provider-settings,voice-player}.html` 与 `voice-player-notices.txt` 由 Dockerfile 构建期 `ui/build.mjs` 生成，严禁重新提交入库。
- **密钥零明文与安全 fail-closed**：
  - `DEEPSEEK_API_KEY`、`GEMINI_API_KEY`、`ESP32_AUTH_KEY` 等仅由环境变量/占位符 `${ENV_VAR}` 在内存展开，严禁落盘明文或回显日志；严禁硬编码盘符路径、用户名或固定局域网 IP。
  - `security` 类补丁（`config_env_expand`、`esp32_auth_ota`、`esp32_auth_ws`、`esp32_auth_vision`、`mcp_token_injection`）失败或契约不匹配时必须拒绝启动（`sys.exit(2)`；仅显式设置 `XIAOZHI_ALLOW_INSECURE_BOOT=1` 应急放行，且 `/api/health` 持续返回 `503` + `18080` 挂红横幅）。`experience` 类补丁契约不匹配时跳过挂载（`skipped_contract`），提示词锚点缺失时逐条软跳过（`partial`），`/api/health` 标注 `degraded`。
  - 严禁修改宿主机 Antigravity `user_config.json`/`config.json`；Sidecar 若指向 `language_server.exe`，首参必须显式传 `"agentapi"` 且 `<prompt>` 前不加 `--`。
- **硬件刷机与隐私库热备**：ESP32-S3 执行 `write-flash` 前必先备份 `0x9000` NVS 与引导区至 `backups/`；手工维护 `anima-data/brain.sqlite3` 前必先停止 `xiaozhi-anima-brain` 并备份至 `backups/`（严禁宿主机直开 WAL 模式库文件）。
- **GitHub 公开仓库脱敏拦截**：每次提交前必跑 `git add -n .`，严禁将 `.env`、`*.bin`、`secrets/`、`backups/`、`anima-data/`、`state/`、`config/runtime/`、`voices/`、`models/gsv_weights/`、`pc-mcp/state/`、`logs/`、`.venv`、截图或私人路径/MAC/密钥推入版本库；保持 `.gitattributes` 锁定 `*.bat`/`*.ps1` 为 `eol=crlf`。

---

## 2. 核心防回归清单

修改对应子系统时必须保持以下目标不退化：

- **模块化启动与补丁契约（H-1 / C-1..3 / I-1 / P3-3）**：唯一入口为 `app/boot.py`（导入零副作用：不 `chdir`、不起线程、不打补丁），经 `install_all()` 显式挂载 `app/patches/`（含 `patches/emotion/` 与 `cloud_audio.py`）全部 **21 项**补丁并执行轻量契约自检写入注册表；`app/console/` 拆分为 `server.py`、`shared.py`、`routes/*` 与 `static/{index.html,app.css,app.js}`；增删补丁必须同步更新 `app/patches/registry.py`（`PATCH_CATALOG`）并重新生成 `docs/patches.md`。
- **长期记忆零丢轮与双端同步（B-1 / B-5 / C-6 / P1-2 / P2-4）**：保留 `anima-brain` `brain.sqlite3` 持久化 `xiaozhi_inbox`（事务提交后返 200、重启自动恢复、`scripts/rollback_inbox.py`）、容器 SQLite Outbox（死信 8 次 / 14 天 TTL / `18080` 重放按钮）、按 `device_id` 严格隔离的 300ms 温召回与多设备独立角色映射（`device_character_map` / `isolate_character_by_device`，自动初始化独立 `character_id`、不偏向单人跨房间混串）、中文 `episode`/`fact` 分词 FTS5 索引与 120ms 有界 LRU 查询向量、`18080` 保存自动同步人设，以及 Obsidian 单向原子导出（SQLite 触发器序号 `O(1)` 轮询短路，保证 `/data/anima` 与 `/obsidian` 对 uid `1000` 可写）。
- **防抢话 VAD 与投机早判（F-1 / P1-4）**：保留 `SileroVAD` `min_silence_duration_ms: 1100` 与 `threshold_low: 0.2` 硬上限；保留 `app/vad_speculative_router.py`（~550ms 静音独立 `asr` 线程预解码、1100ms 截止复用在途解码任务、唤醒首句 800ms、犹豫/讲故事自动续等至 1100ms、续说即弃，可经 `18080` `/api/vad-speculative` 一键热关回退纯 1100ms）。
- **轻量 ASR、同音纠错与内存治理（G-1..4 / F-3 / H-6 / P1-4）**：默认 `SherpaASR`（`model.int8.onnx` 纯内存 PCM 直解，免临时 WAV 落盘）+ `modelscope` 懒加载 + 独立单线程 `asr` 执行器 + `asr_corrections.py` 热更新同音纠错表（云端/本地不重复二次纠错）+ 每轮空闲 25s `gc.collect()`/`malloc_trim(0)` + glibc 环境变量（`MALLOC_ARENA_MAX=2` 等）；实测双容器运行态内存 `xiaozhi-esp32-server ~412–416 MiB`（`VmRSS ~457 MiB`）、`xiaozhi-anima-brain ~67–84 MiB`（较旧版 `FunASR` `3.54 GB` 压降 ~88%）；保留 `19880` 闲置回收阈值 `75%`、`/prime` 预热、唤醒异步预热与 `5 MB × 3` 日志轮转。
- **语音情绪路由、云端音频加固与播音桥接（C-4/5/7/8 / F-2 / P0-1..3 / P1-1..5）**：保留 7 情绪动态路由（本地 GSV、云端流式/HTTP TTS 与 `EdgeTTS` 全通道逐句表情同步、防吞括号与 XML/DSML 标签清洗）、试听权重 `finally` 自动回滚、唤醒缓存不可变快照与异步锁、空闲连接热切换云端 ASR/TTS、`pc_audio_tts` 先于 `mei_emotion_and_prosody` 且 `mei_wakeup_responses` 晚于 `pc_audio_playback` 的挂载顺序、WinMM 3 缓冲直出、空闲告别双模式（隔离 TTFT 遥测）、`turn_context_router` 前缀缓存分离/回滚、`reply_length_router`/亲密度热调、`audio_api`/`cloud_audio` `ipaddress` SSRF 与零重定向/密钥脱敏门禁，以及 `nmc_weather` 免 Key 天气补丁（国内直连 `NO_PROXY` + `socksio` 代理兼容 + 指数退避）。
- **控制台与宿主机 MCP 加固（A-2..4 / D-1..7 / E-1..4 / H-5..9 / P2-1..5）**：操控挡位唯一权威在宿主机 `pc-mcp/state/pc_control_mode.json`；`18080` 强制随机 `X-Console-Token`、严格 `Content-Security-Policy`、内部 `Host` 仅放行只读接口、写锁与 400 白名单校验、无面板访问时探活自动退避至 30s、2s 异步刷盘、256KB 日志上限与 `_send_json` 脱敏；`18900` 保留 L2 `FAILSAFE=True` + `Ctrl+Alt+Pause/Break` 急停 + 多屏负坐标窗口截取 + 资源管理器/终端/运行框拦截 + 区域哈希防误点 + `l2_audit.jsonl` 5MB×3 轮转、看门狗重启保活 L3 与 10 分钟 3 次熔断、L3 同目录互斥/排队/取消（含 Antigravity `dispatched` 活跃 `transcript.jsonl` 守卫）、公共工具统一于 `pc-mcp/common/` 与 `xz_common.py`，以及 `pc-mcp/requirements.in` → hash 锁定 `requirements.txt`（含 `socksio`）。
- **可复现镜像与离线测试分层（H-2 / H-3 / H-4 / H-8）**：`anima-brain/Dockerfile` 锁定双阶段镜像 `@sha256` digest + Py3.12 `constraints.txt` + 非可编辑安装 + `/health` 返回 `build_version`；容器测试统一放在 `tests/container/`（不入生产挂载 `app/`），`unit` 与 `contract` 层走临时 `docker run --rm`，零运行中容器依赖。

---

## 3. 必跑验证命令

```powershell
# 1. 全链路三层回归（当前 250 项：unit 234 + contract 7 + live 9；另有 Anima 镜像内 257 项 pytest 回归）
python scripts/run_tests.py --layer all

# 2. 离线单测 / 上游镜像契约（临时 docker run --rm，无需运行中容器；升级上游镜像必跑 contract）
python scripts/run_tests.py --layer unit
python scripts/run_tests.py --layer contract

# 3. 补丁文档与 PATCH_CATALOG 强校验（增改补丁后先不带 --check 重新生成）
python scripts/generate_patches_doc.py --check

# 4. 宿主机 pc-mcp 单测 / 提交前脱敏检查
pc-mcp\.venv\Scripts\python.exe -m unittest discover -s pc-mcp -p "test_*.py" -v
git add -n .
```
