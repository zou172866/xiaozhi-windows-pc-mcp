# 小智 ESP32 本地服务端与三层电脑操控规范 (xiaozhi-server/AGENTS.md)

操作本目录（`e:\故事\1\xiaozhi-server`）下的配置、容器、语音合成与电脑操控 MCP 服务时，必须遵守以下硬性约束：

## 1. 端口与进程守护红线
- **严禁回退至 `15000` 以下宿主机端口**（避开 Windows Hyper-V 动态保留端口段随机征用）：
  - `18000`：小智 WebSocket 语音主通道（容器内 `8000`）
  - `18003`：ESP32 OTA 固件配置接口（容器内 `8003`）
  - `18080`：本地 Web 控制台（挡位开关 / 模型切换 / 音色热切换）
  - `18787`：`Anima Brain` 长期记忆与情绪大脑容器
  - `18900`：宿主机 Windows 三层电脑操控 MCP 服务（`streamable-http`）
  - `19880`：宿主机 `RTX 5070 Ti` `GPT-SoVITS` 语音合成引擎
- **无黑框交互桌面启动**：宿主机后台服务（`18900`、`19880`）必须通过 WMI `Win32_Process.Create` 配合 `Win32_ProcessStartup`（`ShowWindow = [uint16]0`）启动，确保挂载在真实交互桌面 `WinSta0\Default`（具备截屏与窗口前置能力）且不在任务栏留下可被误关的黑色 `cmd` 窗口。
- **精准停止单进程**：重启或关闭 `18900` MCP 服务时，使用 `Stop-Process -Id <OwningProcess> -Force`，严禁使用 `taskkill /T` 连带强杀由小智唤起的用户浏览器窗口。
- **批处理编码铁律**：所有 `.bat` 启动/停止入口文件必须保持**纯 ASCII 字符 + CRLF 换行**（防 `cmd.exe` 多字节偏移截断），中文输出与逻辑一律放入带 UTF-8 BOM 的 `.ps1` 脚本。
- **权限边界与控制面加固（A-1 / A-2 / A-3 / A-4）**：
  - `docker-compose.yml` 按职责拆分挂载：`app/`、`config/`、`voices/` 与 `.mcp_server_settings.json` 只读挂入主容器，`config/runtime/` 挂至 `/opt/xiaozhi-esp32-server/data`，`state/` 挂至 `/opt/xiaozhi-esp32-server/state`，`anima-data/` 仅挂入 `anima-brain`；宿主机 `secrets/`、`backups/`、`logs/` 与 `models/gsv_weights/`（`.ckpt`/`.pth`）永不挂入容器，容器内令牌按最小权限拆分独立 `env_file` 注入（`secrets/anima_tokens.env` 供双容器，`secrets/pc_mcp_tokens.env` 仅注入主容器，`anima-brain` 严禁接触 `PC_MCP_TOKEN`）；
  - 电脑操控挡位仅以宿主机私有文件 `pc-mcp/state/pc_control_mode.json` 为唯一权威（严禁读取容器可写目录）；`18080` 切挡走带 Bearer 令牌的宿主机 `POST /mode` 接口并记录 `l2_audit.jsonl` 审计日志；
  - `18900` L2 护栏默认开启 `pyautogui.FAILSAFE=True` 与全局急停热键 `Ctrl+Alt+Pause/Break`（同时注册 `VK_CANCEL` 与 `VK_PAUSE` 适配 Windows 物理键与虚拟键），拦截资源管理器地址栏/搜索框（含 Win10/11 XAML Islands UIA 与 `Alt+D`/`Ctrl+L`/`F3`/`F4`/`Ctrl+E`/`Ctrl+F`）、开始菜单/搜索主机与任务管理器「创建新任务」，右键点击检查前台与目标坐标终端窗口，剪贴板输入后无论原是否为空一律还原或清空；
  - `18080` 每次启动生成随机写令牌注入页面模板，所有 `POST` 接口强制校验 `X-Console-Token`；`Host: xiaozhi-esp32-server` 仅放行只读 `/api/health` 与 `/api/state`。

## 2. 模型能力与密钥安全约束
- **密钥零明文落盘**：`DEEPSEEK_API_KEY` 与 `GEMINI_API_KEY` 仅从系统环境变量读取，配置文件中一律保留 `${DEEPSEEK_API_KEY}` / `${GEMINI_API_KEY}` 占位符由 `app/start_with_env.py` 在内存展开，严禁将明文密钥写入任何磁盘文件、日志或终端回显。
- **`deepseek-flash` 原生多模态视觉认知**：`deepseek-flash`（DeepSeek V4.1 Flash，`https://api.deepseek.com/chat/completions`）**原生支持 `image_url` 多模态视觉输入**（注意图片块必须置于 `role: "user"` 消息内，配合 `"thinking": {"type": "disabled"}` 实现国内 ~1.5s 极速识图）。在工具层执行屏幕识图或视觉坐标定位时，默认优先调用 `deepseek-flash` 并与 `gemini-3.5-flash-lite` 自动互备（跟随控制台主模型选择）。
- **Antigravity Sidecar 安全边界**：严禁直接修改宿主机 Antigravity 的 `user_config.json` / `config.json` 绕过权限检查；通过官方 Sidecar 注入的 `ANTIGRAVITY_AGENTAPI_EXE` 若指向 `language_server.exe`，调用子命令首参必须显式传入 `"agentapi"`（且 `<prompt>` 前不加 `--`）。

## 3. 核心模块防回归清单
修改 `config/runtime/.config.yaml`、`docker-compose.yml` 或 `app/start_with_env.py` 时，必须完整保留以下已调优能力：
- **长期记忆共存、持久化收件箱与双端人设同步（B-1 / B-5 / C-6）**：
  - 保留 `Memory: AnimaBrain`（`http://anima-brain:18787`）、`app/start_with_env.py` 记忆钩子以及 `docker-compose.yml` 中 `./anima-brain/xiaozhi_anima_service.py:/app/xiaozhi_anima_service.py:ro` 挂载（含 `brain.sqlite3` 持久化 `xiaozhi_inbox` 表在事务 `INSERT OR IGNORE` 提交后再返回 HTTP 200、启动及运行期自动恢复 `pending`/超时 `processing` 行实现重启零丢轮、`/xiaozhi/health` 暴露 `inbox_pending`/`inbox_dead`、`anima-brain/scripts/rollback_inbox.py` 安全回滚脚本、从 `turn_appraisals` 读取真实历史情绪、`install_gemini_embedding_compat_patch()` 兼容 Gemini `/embeddings` 省略 `index` 键防止 `memory_jobs` 卡死在 `review`、启动自动清理超时 `review` 锁，以及记忆/待办带日期防复读）；
  - `app/anima_memory_provider.py` 保留本地 SQLite Outbox 死信与 TTL 治理（HTTP 4xx 或指数退避重试超 8 次转 `dead` 停止无限重试、`dead`/`corrupt` 超过 14 天自动清理、向 `18080` 暴露 `get_outbox_stats()` 与 `replay_dead_outbox_turns()`「重放 dead」按钮），按 `device_id` 隔离召回缓存并同步传递 `device_id` 给 `mei_emotion_router.set_anima_mood()`（温缓存命中仍保留 300ms 短预算同步召回、超时回退缓存并后台刷新，避免“慢一轮”），召回时必须剥离重复的 `【角色身份设定】` 段落（以 `18080` 主 `prompt` 为唯一人设权威），且 `app/console_server.py` 保存配置时必须自动调用 `/api/update_character` 双端同步角色 `name` 与 `persona`；
- **防抢话 VAD 上限与投机性句末早判（F-1）**：保留 `VAD.SileroVAD` 的 `min_silence_duration_ms: 1100` 与 `threshold_low: 0.2` 作为硬上限（严禁回退至上游默认的 `200ms` 导致用户句中换气被截断）；保留 `app/vad_speculative_router.py`（通过 `patch_registry` 以 `experience` 契约校验挂载），静音约 `550ms`（唤醒首句保守 `800ms`）在独立 `asr` 线程触发内存态 Sherpa 预解码，语义完整则提前提交并复用解码结果（零重复识别、平均省 ≥0.3s 尾延迟），命中犹豫/未完词或讲故事/听写模式自动续等至 `1100ms`，续说立即丢弃；参数与总开关由 `app/runtime_settings.py` 与 `18080` `/api/vad-speculative` 免重启热调（可一键关闭立即回退纯 `1100ms`）。
- **免 Key 天气补丁（C-7）**：保留 `app/nmc_weather_patch.py`（中央气象台 `nmc.cn` 实时与 7 天预报，默认城市 `长沙`，过滤 `9999` 缺测哨兵值，站点抓取失败按 `5min→1h` 指数退避、复用模块级 `httpx.AsyncClient`、有效本地 `nmc_stations_cache.json` 无条件优先），不回退上游已 `401` 失效的和风测试 Key；
- **轻量语音识别、同音纠错与冷内存治理（F-3 / G-1 / G-2 / G-3 / G-4）**：
  - 默认 `selected_module.ASR` 保持 `SherpaASR`（`models/sherpa-onnx-sense-voice/model.int8.onnx`）并保留 `app/start_with_env.py` 中的 `modelscope` 懒加载守卫、独立单线程 `ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")` 异步 `decode_stream`（G-2）、基于 `runtime_events` 订阅的每轮结束空闲 `25s` 触发 `gc.collect()` + `malloc_trim(0)`（G-1，避开活跃对话窗口，容器常驻内存由 `3.54 GB` 降至 `~400 MB`，切勿无故回退 `FunASR` 拖入 PyTorch），以及 `app/asr_corrections.py` + `config/runtime/asr_corrections.json` + `18080` `/api/asr-corrections` 热更新同音词后处理纠错表（G-4，默认含 `牙医->芽衣` / `雷电牙医->雷电芽衣`）；
  - 保留 `docker-compose.yml` 中的 `MALLOC_ARENA_MAX=2`、`MALLOC_TRIM_THRESHOLD_=131072`、`MALLOC_MMAP_THRESHOLD_=131072` 与 `OMP_NUM_THREADS=2`；
  - 保留 `data/start_all.ps1` 中 `19880` 进程的 `GSV_IDLE_TRIM_SECONDS=300` + `GSV_IDLE_TRIM_MEM_THRESHOLD=75`（仅在宿主机内存占用 `>=75%` 时触发 `EmptyWorkingSet`）、启动及进程内 `5 MB x 3` `RotatingFileHandler` 日志轮转（H-6，`--log_file` 避开 `cmd >` 句柄锁）、启动调用 `POST /prime` 预热 7 种情绪参考缓存并对短于 v4 `T_ref`（`5.0s`）的情绪做 `sample_steps=4` 极短 cuDNN 形状暖机、唤醒/握手异步 `prime_gsv_async()`，以及 `%UserProfile%\.wslconfig` 的 `[experimental] autoMemoryReclaim=gradual` 持续内存回收（不再一次性执行 `wsl drop_caches`）；保留 `gpt-sovits-studio` 默认 `G2PWTorchConverter` 与 `TTS.py` 权重幂等加载；
- **运行期管线、控制台与宿主机 MCP 稳定性加固（C-4/5/8、D-1..D-7、E-1..E-4、H-6/7）**：
  - 核心管线：`app/mei_emotion_router.py` 唤醒缓存刷新使用不可变请求快照且仅在 GSV 空闲时经 `async with` 执行（零污染共享 TTS，C-4），异步函数内阻塞 HTTP/写盘经 `asyncio.to_thread` 卸载、`app/pc_audio_router.py` 对 `queue_empty_event` 做有界防卡死等待（C-5），`app/start_with_env.py` 用事件循环 `call_later` + 快照副本替代每轮 `threading.Timer`（C-8）；
  - `18080` Web 控制台：聊天记录 2s 合并异步刷盘（零同步 fsync，D-1）、`.config.yaml` 写锁与严格 400 白名单校验（D-2）、单次 256KB 日志上限/5s 游标落盘节流/YAML `MtimeFileCache`/切后台暂停轮询（D-4）、`_send_json` 统一密钥脱敏（D-5）、限频异常日志（D-6）与持久化显式 `voice_id`（D-7）；
  - 宿主机 `18900` MCP：看门狗 3 次×5s 探活/重启仅停 MCP 不杀运行中 L3 子进程/10 分钟 3 次重启熔断告警/`boot.log` 追加轮转（E-1）、L3 同工作目录单任务互斥与排队/取消（E-2）、视觉定位截图后立即释放 `_gui_lock` 并在点击前比对目标区域哈希防误点（E-3）、Sidecar 独立心跳线程与无 `--` 前缀破折号防护（E-4）、`l2_audit.jsonl` 5MB×3 轮转/`agy_tasks/active` 仅保留最新 50 条/`transcript.jsonl` 仅读尾 64KB（H-6）以及 L1 开始菜单敏感工具/卸载器黑名单与目录 mtime 索引缓存（H-7）。
- **Windows 电脑音响 48kHz 直通桥接**：保留 `app/pc_audio_router.py`（须于 `mei_emotion_router` 之前挂载 `apply_pc_audio_to_tts_patch`、之后挂载 `apply_pc_audio_playback_patches`）、`pc-mcp/pc_control_mcp.py` 复用的 `18900` 端口 `/audio/*` WinMM 无损播放接口，以及 `18080` 控制台免重启切换开关（`state/runtime_settings.json`）。
- **可插拔 L3 后端与四处本机硬编码解耦**：
  - 保留 `pc-mcp/l3_backends/`（`antigravity` / `dsh` / `workbuddy` / `none` 四选一适配器）、`pc_control_mcp.py` 的 `pc_agent_task` + `/l3/status` + `/l3/config`，以及 `18080` 控制台 `/api/l3-backend` 免重启切换面板；
  - 严禁重新写死盘符路径（`e:\故事\1`）、用户名（`Administrator`）或固定局域网 IP（`192.168.31.200`）；一律通过 `Path(__file__)` 动态推导、`.env`（`HOST_LAN_IP`、`HOST_PROJECT_DIR`、`HOST_DATA_DIR`、`HOST_VOICES_DIR`、`HOST_GSV_WEIGHTS_DIR`、`ENABLE_LOCAL_GSV`、`ANIMA_*`）与 `${ENV_VAR}` 占位符注入，`data/start_all.ps1` 在无本地 GPU `GPT-SoVITS` 时必须自动优雅跳过 `19880`。
- **LLM 前缀缓存稳定化、回复字数与亲密度免重启热调（F-2）**：
  - 保留 `app/turn_context_router.py`（`turn_context_mode: split|legacy` 可一键回滚开关），在 `split` 模式下将时间/天气 `<context>`、`<memory>`、`<reply_length_control>`、`<speakers_info>` 统一剥离出 `system` 并以单块 `<turn_context>` 紧贴最新一条用户消息注入（历史轮不残留旧 `<turn_context>`），同时在 LLM 流式补丁中采集 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 与 TTFT 并在 `18080` 控制台展示命中率；
  - 保留 `app/reply_length_router.py`（`state/runtime_settings.json`，`18080` 控制台 `/api/reply-length` 双滑块 + 4 档预设，免重启实时调整目标字数并动态扩展 `max_tokens`）；
  - 保留 `anima-brain/xiaozhi_anima_service.py` 的 `GET/POST /xiaozhi/relationship` 与中文互动尺度注入，以及 `18080` 控制台 `/api/relationship` 亲密度热调卡片（克制助手 / 温柔知己 / 亲密依恋 3 档预设、`Romance` 开关、熟悉/温暖/信任/浪漫 4 滑块，无滞回卡顿、下一句话立即生效）。
- **补丁失败行为红线（C-1 / C-2 / C-3）**：
  - `security` 类补丁（`esp32_auth_*`、`mcp_token_injection`）失败或契约不匹配时拒绝启动（`sys.exit(2)`，仅显式设置 `XIAOZHI_ALLOW_INSECURE_BOOT=1` 可应急放行并在 `18080` 挂红色横幅且 `/api/health` 返回 `503`）；
  - 任意补丁签名契约校验失败时严禁挂载替换原函数（记录 `skipped_contract`）；提示词锚点缺失时逐条跳过软失败（每锚点仅告警一次，注册 `partial` 状态使 `/api/health` 返回 `200` + `degraded`）。

## 4. 雷电芽衣 7 情绪动态语音与流式切句规范
- **防流式吞括号与 XML 工具标签漏念**：保留 `app/mei_emotion_router.py` 对 `ConnectionHandler._clean_response_garbage`、`PromptManager.build_enhanced_prompt` 及 `_inject_tool_call_fewshot` 的补丁，严防 `direct_answer` 分块边界误删 `[gentle]` 右括号 `]`，并持续通过 `_LEAKED_XML_TOOL_TAG_RE` 清洗 LLM 偶发吐出的 `</parameter>` / `</invoke>` / `</tool_call>` 残留标签，同时保留 `[安全挡]` 等正常正文方括号。
- **整句连贯合成**：保持 `TTS.GPT_SOVITS_V2.text_split_method: cut0`、`parallel_infer: true`（v4 `batch_size: 1` 下实测生效，已清理死键 `split_bucket`/`batch_threshold`）与外层防首句 2～3 字短逗号强切逻辑；普通句尾 `～` 不得覆盖本轮显式情绪标签。
- **播放级表情同步与试听保护**：ESP32 屏幕表情必须经 `_segment_emotion_queue` 在 `send_tts_message("sentence_start")` 阶段按 `AudioRateController` 真实播放进度发送（严禁在合成线程提前推送）；`18080` 控制台 `/api/tts-preview` 试听非当前 `GPT-SoVITS` 音色后，必须在 `finally` 块自动还原已保存的线上音色权重。
- **必跑回归自检套件**：修改 TTS、情绪路由、补丁注册、控制台、安全鉴权、唤醒词/打断词回复、投机 VAD、ASR 纠错、回复字数、亲密度、L3 后端、提示词或记忆代码后，必须运行统一三层测试入口 `python scripts/run_tests.py --layer all`（按 `unit` / `contract` / `live` 三层覆盖全部套件），或按模块分别运行：容器离线/在线回归 `docker exec xiaozhi-esp32-server python3 /opt/xiaozhi-esp32-server/app/test_mei_emotion_and_prosody.py`（含 A-1 目录隔离与环境变量令牌、A-2 宿主机切挡代理、A-4 控制台 `X-Console-Token` 与内部 Host 限权、F-2 `<turn_context>` 前缀缓存分离/回滚与 usage 遥测、F-3 唤醒/握手异步 `/prime`、安全补丁 fail-closed 启动门禁、Anima Outbox 冲突保护与试听超时/脏音色恢复等用例）、Batch 3 容器套件 `app/test_batch3_anima_outbox.py`（B-5 死信/TTL/重放与 C-6 按设备隔离 300ms 温召回）、`app/test_batch3_pipeline_vad.py`（C-4..C-8、F-1 34 条句中换气零误截断回归与 20 条尾延迟基准、G-1 空闲回收与 glibc 环境变量、G-2 独立 ASR 线程与 G-4 同音词热纠错）、`app/test_batch3_console.py`（D-1..D-7、H-6 与 B-5/F-1/G-4 接口/令牌/DOM 校验）、上游镜像契约测试 `docker exec xiaozhi-esp32-server python3 /opt/xiaozhi-esp32-server/app/test_upstream_contracts.py`、Anima Brain 隔离单测（`test_xiaozhi_service.py` 含 B-1 崩溃重启零丢轮对账与 `rollback_inbox.py` 回滚验证 + `test_obsidian_exporter.py`）及宿主机 `pc-mcp\.venv\Scripts\python.exe -m unittest discover -s pc-mcp -p "test_*.py"`（覆盖 A-2/A-3 护栏、E-1 看门狗重启保活 L3 与熔断、E-2 同目录互斥/排队/取消、E-3 视觉锁拆分与区域哈希防误点、E-4 Sidecar 心跳与防 `-` 参数注入、H-6/H-7 审计轮转/64KB 尾读/开始菜单黑名单与 mtime 缓存）确认全通过。
- **自定义唤醒词、离线打断词与芽衣专属秒回语**：硬件已升级至 `bread-compact-wifi v2.6.0`（`0.91" 128x32`、`partitions/v2/16m.csv`），`0x800000` `assets` 分区搭载原厂 `esp-sr 2.4.7` 的 `mn7_cn` + `fst` 模型并并行配置两条离线命令词：`ni hao ya yi`（`你好芽衣`）与 `ting wo shuo`（`听我说`，`threshold=0.2`）；服务端保留 `config/runtime/.config.yaml` 的 `wakeup_words` 与 `interrupt_words` 列表，以及 `app/start_with_env.py` 在 `pc_audio_router.apply_pc_audio_playback_patches()` 之后挂载的 `mei_emotion_router.apply_mei_wakeup_responses_patch()`（同步覆写 `core.handle.intentHandler.checkWakeupWords` 与 `helloHandle.handleHelloMessage` 防首声回退并触发异步 `/prime`，同时拦截 `ListenTextMessageHandler` 打断词 `detect` 实现零寒暄、零 2 秒 VAD 屏蔽直通聆听）。
- **硬件刷机与隐私库热备铁律**：对 ESP32-S3 执行 `write-flash` 前必先备份 `0x9000` NVS（16KB）与 `0x0..0x10000` 引导区至 `backups/`；直接维护 `anima-data/brain.sqlite3` 前必先停止 `xiaozhi-anima-brain` 容器并留存数据库快照至 `backups/`。

## 5. GitHub 公开仓库同步与隐私红线
- **远端公开仓库**：`https://github.com/zou172866/xiaozhi-windows-pc-mcp`（推送更新时保持 `.gitattributes` 锁定 `*.bat` / `*.ps1` 为 `eol=crlf`；引入第三方目录前先检查是否存在嵌套 `.git` 防止产生 `160000` 空目录）。
- **公网脱敏硬拦截**：提交前必跑 `git add -n .` 核验，严禁将 `*.bin`（含家庭 Wi-Fi 凭证的 ESP32 NVS 备份）、`secrets/`、`backups/`、`anima-data/`（`brain.sqlite3` 隐私记忆库与访问令牌）、`state/`（`chat_history.json` / `runtime_settings.json` / `anima_outbox.sqlite*`）、`config/runtime/`、`voices/`、`models/gsv_weights/`、`pc-mcp/state/`、`logs/`、屏幕截图、`.venv` 或百兆模型音频文件推入版本库。
