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
- **长期记忆共存**：保留 `Memory: AnimaBrain`（`http://anima-brain:18787`）与 `start_with_env.py` 中的异步记忆同步钩子；
- **防抢话 VAD 阈值**：保留 `VAD.SileroVAD` 的 `min_silence_duration_ms: 1100` 与 `threshold_low: 0.2`（严禁回退至上游默认的 `200ms` 导致用户句中换气被截断）；
- **免 Key 天气补丁**：保留 `data/nmc_weather_patch.py`（中央气象台 `nmc.cn` 实时与 7 天预报，默认城市 `长沙`，过滤 `9999` 缺测哨兵值），不回退上游已 `401` 失效的和风测试 Key；
- **冷内存自动裁剪**：保留 `data/start_all.ps1` 启动末尾对 `19880` 进程的 `EmptyWorkingSet` 冷页回收与 WSL2 `drop_caches`。
