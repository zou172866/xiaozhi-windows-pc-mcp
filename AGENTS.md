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

## 2. 模型能力与密钥安全约束
- **密钥零明文落盘**：`DEEPSEEK_API_KEY` 与 `GEMINI_API_KEY` 仅从系统环境变量读取，配置文件中一律保留 `${DEEPSEEK_API_KEY}` / `${GEMINI_API_KEY}` 占位符由 `data/start_with_env.py` 在内存展开，严禁将明文密钥写入任何磁盘文件、日志或终端回显。
- **`deepseek-flash` 原生多模态视觉认知**：`deepseek-flash`（DeepSeek V4.1 Flash，`https://api.deepseek.com/chat/completions`）**原生支持 `image_url` 多模态视觉输入**（注意图片块必须置于 `role: "user"` 消息内，配合 `"thinking": {"type": "disabled"}` 实现国内 ~1.5s 极速识图）。在工具层执行屏幕识图或视觉坐标定位时，默认优先调用 `deepseek-flash` 并与 `gemini-3.5-flash-lite` 自动互备（跟随控制台主模型选择）。
- **Antigravity Sidecar 安全边界**：严禁直接修改宿主机 Antigravity 的 `user_config.json` / `config.json` 绕过权限检查；通过官方 Sidecar 注入的 `ANTIGRAVITY_AGENTAPI_EXE` 若指向 `language_server.exe`，调用子命令首参必须显式传入 `"agentapi"`（且 `<prompt>` 前不加 `--`）。

## 3. 核心模块防回归清单
修改 `data/.config.yaml`、`docker-compose.yml` 或 `data/start_with_env.py` 时，必须完整保留以下已调优能力：
- **长期记忆共存与双端人设同步**：
  - 保留 `Memory: AnimaBrain`（`http://anima-brain:18787`）、`start_with_env.py` 记忆钩子以及 `docker-compose.yml` 中 `./anima-brain/xiaozhi_anima_service.py:/app/xiaozhi_anima_service.py:ro` 挂载（含从 `turn_appraisals` 读取真实历史情绪、`install_gemini_embedding_compat_patch()` 兼容 Gemini `/embeddings` 省略 `index` 键防止 `memory_jobs` 卡死在 `review`、启动自动清理超时 `review` 锁，以及记忆/待办带日期防复读）；
  - `data/anima_memory_provider.py` 召回时必须剥离重复的 `【角色身份设定】` 段落（以 `18080` 主 `prompt` 为唯一人设权威），且 `data/console_server.py` 保存配置时必须自动调用 `/api/update_character` 双端同步角色 `name` 与 `persona`；
- **防抢话 VAD 阈值**：保留 `VAD.SileroVAD` 的 `min_silence_duration_ms: 1100` 与 `threshold_low: 0.2`（严禁回退至上游默认的 `200ms` 导致用户句中换气被截断）；
- **免 Key 天气补丁**：保留 `data/nmc_weather_patch.py`（中央气象台 `nmc.cn` 实时与 7 天预报，默认城市 `长沙`，过滤 `9999` 缺测哨兵值），不回退上游已 `401` 失效的和风测试 Key；
- **轻量语音识别与冷内存治理**：
  - 默认 `selected_module.ASR` 保持 `SherpaASR`（`models/sherpa-onnx-sense-voice/model.int8.onnx`）并保留 `data/start_with_env.py` 中的 `modelscope` 懒加载守卫、异步 `decode_stream` 与定期 `malloc_trim(0)`（容器常驻内存由 `3.54 GB` 降至 `~400 MB`，切勿无故回退 `FunASR` 拖入 PyTorch）；
  - 保留 `docker-compose.yml` 中的 `MALLOC_ARENA_MAX=2` 与 `OMP_NUM_THREADS=2`；
  - 保留 `data/start_all.ps1` 中 `19880` 进程的 `GSV_IDLE_TRIM_SECONDS=60` 闲置自动回收、预热后 `EmptyWorkingSet` 与 WSL2 `drop_caches`，以及 `gpt-sovits-studio` 中默认 `G2PWTorchConverter`（CUDA FP16 替代 CPU `g2pW.onnx`）与 `TTS.py` 权重幂等加载；
- **Windows 电脑音响 48kHz 直通桥接**：保留 `data/pc_audio_router.py`（须于 `mei_emotion_router` 之前挂载 `apply_pc_audio_to_tts_patch`、之后挂载 `apply_pc_audio_playback_patches`）、`pc-mcp/pc_control_mcp.py` 复用的 `18900` 端口 `/audio/*` WinMM 无损播放接口，以及 `18080` 控制台免重启切换开关（`data/audio_output_mode.json`）。
- **回复字数与亲密度免重启热调**：
  - 保留 `data/reply_length_router.py`（`data/reply_length_config.json`，`18080` 控制台 `/api/reply-length` 双滑块 + 4 档预设，免重启实时注入 `<reply_length_control>` 并动态扩展 `max_tokens`）；
  - 保留 `anima-brain/xiaozhi_anima_service.py` 的 `GET/POST /xiaozhi/relationship` 与中文互动尺度注入，以及 `18080` 控制台 `/api/relationship` 亲密度热调卡片（克制助手 / 温柔知己 / 亲密依恋 3 档预设、`Romance` 开关、熟悉/温暖/信任/浪漫 4 滑块，无滞回卡顿、下一句话立即生效）。

## 4. 雷电芽衣 7 情绪动态语音与流式切句规范
- **防流式吞括号与 XML 工具标签漏念**：保留 `data/mei_emotion_router.py` 对 `ConnectionHandler._clean_response_garbage`、`PromptManager.build_enhanced_prompt` 及 `_inject_tool_call_fewshot` 的补丁，严防 `direct_answer` 分块边界误删 `[gentle]` 右括号 `]`，并持续通过 `_LEAKED_XML_TOOL_TAG_RE` 清洗 LLM 偶发吐出的 `</parameter>` / `</invoke>` / `</tool_call>` 残留标签，同时保留 `[安全挡]` 等正常正文方括号。
- **整句连贯合成**：保持 `TTS.GPT_SOVITS_V2.text_split_method: cut0` 与外层防首句 2～3 字短逗号强切逻辑；普通句尾 `～` 不得覆盖本轮显式情绪标签。
- **播放级表情同步与试听保护**：ESP32 屏幕表情必须经 `_segment_emotion_queue` 在 `send_tts_message("sentence_start")` 阶段按 `AudioRateController` 真实播放进度发送（严禁在合成线程提前推送）；`18080` 控制台 `/api/tts-preview` 试听非当前 `GPT-SoVITS` 音色后，必须在 `finally` 块自动还原已保存的线上音色权重。
- **必跑回归自检套件**：修改 TTS、情绪路由、唤醒词/打断词回复、回复字数、亲密度、提示词或记忆代码后，必须运行 `docker exec xiaozhi-esp32-server python3 /opt/xiaozhi-esp32-server/data/test_mei_emotion_and_prosody.py` 确认 21 项测试全通过。
- **自定义唤醒词、离线打断词与芽衣专属秒回语**：硬件已升级至 `bread-compact-wifi v2.6.0`（`0.91" 128x32`、`partitions/v2/16m.csv`），`0x800000` `assets` 分区搭载原厂 `esp-sr 2.4.7` 的 `mn7_cn` + `fst` 模型并并行配置两条离线命令词：`ni hao ya yi`（`你好芽衣`）与 `ting wo shuo`（`听我说`，`threshold=0.2`）；服务端保留 `data/.config.yaml` 的 `wakeup_words` 与 `interrupt_words` 列表，以及 `start_with_env.py` 在 `pc_audio_router.apply_pc_audio_playback_patches()` 之后挂载的 `mei_emotion_router.apply_mei_wakeup_responses_patch()`（同步覆写 `core.handle.intentHandler.checkWakeupWords` 防首声回退默认音色，并拦截 `ListenTextMessageHandler` 打断词 `detect` 实现零寒暄、零 2 秒 VAD 屏蔽直通聆听）。
- **硬件刷机与隐私库热备铁律**：对 ESP32-S3 执行 `write-flash` 前必先备份 `0x9000` NVS（16KB）与 `0x0..0x10000` 引导区；直接维护 `data/anima/brain.sqlite3` 前必先停止 `xiaozhi-anima-brain` 容器并留存数据库快照。

## 5. GitHub 公开仓库同步与隐私红线
- **远端公开仓库**：`https://github.com/zou172866/xiaozhi-windows-pc-mcp`（推送更新时保持 `.gitattributes` 锁定 `*.bat` / `*.ps1` 为 `eol=crlf`；引入第三方目录前先检查是否存在嵌套 `.git` 防止产生 `160000` 空目录）。
- **公网脱敏硬拦截**：提交前必跑 `git add -n .` 核验，严禁将 `*.bin`（含家庭 Wi-Fi 凭证的 ESP32 NVS 备份）、`data/anima/`（`brain.sqlite3` 隐私记忆库与访问令牌）、`data/chat_history.json`、`data/agy_tasks/`、屏幕截图、运行日志、`.venv` 或百兆模型音频文件推入版本库。
