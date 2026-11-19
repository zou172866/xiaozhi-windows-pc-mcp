# Xiaozhi ESP32 · Windows 三层电脑操控与长期记忆本地服务端

让 **ESP32-S3 小智语音助手** 直连你的本地 Windows 电脑，具备 **高精度 GUI 视觉点击操控 + Google Antigravity 复杂任务异步派单 + Anima Brain 长期情感记忆 + 本地 GPU `GPT-SoVITS` 定制音色** 的一站式私有化方案。

同时内置沉淀好的 Agent Skill `windows-pc-mcp-automation`，支持通过 `npx skills add` 直接复用到任意 Agent 环境。

---

## 核心特性

### 1. Windows 三层电脑操控架构（`pc-mcp/pc_control_mcp.py`）
- **L1 瞬时系统与视觉感知（日常/游戏安全挡可用，不抢鼠标）**
  - `pc_system_control`：音量调节、静音、切歌/暂停、锁屏、显示器息屏；
  - `pc_open_app_or_web`：内置 64 位 Win32 焦点锁破除引擎（`AttachThreadInput` + `HWND_TOPMOST` Z 轴翻转），语音打开 B站/百度/抖音或本地微信/软件时**自动将窗口恢复并置顶弹出到最前**；
  - `pc_look_at_screen` / `pc_check_system_status`：基于 `deepseek-flash`（DeepSeek V4.1 Flash 原生多模态视觉，~1.5s 极速响应，与 `gemini-3.5-flash-lite` 自动互备）实时看屏答疑、读取 CPU/内存/GPU 负载。
- **L2 高分屏 1:1 物理像素精准键鼠操控（需在 `18080` 控制台开启【全权操控挡】）**
  - 入口强制声明 `SetProcessDpiAwarenessContext(-4)`，彻底解决 Windows `125% / 150% / 200%` 缩放导致的坐标倍差错位；
  - **三阶混合鼠标定位（UIA + Set-of-Mark + 局部放大精修）**：
    1. 明确文字按钮走 **18 层 UIA 无障碍树**直点（`0.15s` 零误差）；
    2. 方位/视觉描述（如“点开第一个视频封面”）自动绘制 **Set-of-Mark 红底数字角标** + 防颠倒 `x_min/y_min/x_max/y_max`（`0~1000` 归一化框）；
    3. 小图标自动触发 `768×512` 1:1 原图裁剪二次精修，并生成带绿十字准星的落点核验图。
- **L3 反重力（Google Antigravity）异步重任务桥接（`pc-mcp/agy_sidecar_worker.py`）**
  - 通过官方 Sidecar 机制与 `agentapi` 通讯，一句话把复杂代码编写、多文件整理、深度网页调研派单给本机已登录的 Antigravity 客户端后台执行，并支持随时语音追问进度。

### 2. 本地 Web 控制台（`http://localhost:18080`）
- 一键实时切换 **关闭 / 日常游戏安全挡 / 全权操控挡**；
- 一键热切换 **DeepSeek API（`deepseek-flash`）** 与 **Gemini API（`gemini-3.5-flash-lite`）**（环境变量占位注入，密钥零明文落盘）；
- 一键热切换本地显卡 **GPT-SoVITS 微调音色** 与 EdgeTTS 音色并支持网页试听。

### 3. 开箱即用的稳定性优化
- **高位端口设计（`>15000`）**：`18000`（WS）、`18003`（OTA）、`18080`（控制台）、`18787`（Anima Brain）、`18900`（PC MCP）、`19880`（GPT-SoVITS），彻底避开 Windows Hyper-V 动态保留端口冲突；
- **防抢话 VAD 调优**：`SileroVAD` 静默断句阈值优化至 `1100ms`，句中自然换气不被打断；
- **免 Key 中央气象台插件（`data/nmc_weather_patch.py`）**：直连 `nmc.cn` 查询全国 34 省级行政区近 2800 个市县实时天气、降雨与 7 天预报，永不过期；
- **无损冷内存回收**：启动后自动裁剪 `GPT-SoVITS` PyTorch CPU 启动期残留冷页缓存（实测立省 `3.5GB+` 物理内存，显卡合成零降速）。

---

## 目录结构

```text
xiaozhi-server/
├── docker-compose.yml               # 小智主服务 + Anima Brain 双容器编排
├── 启动或重启服务.bat                # 纯 ASCII 一键静默启动入口
├── 关闭小智服务.bat                  # 一键停止服务并释放显存
├── 打开网页控制台.bat                # 打开 http://localhost:18080 控制台
├── pc-mcp/
│   ├── pc_control_mcp.py            # L1+L2+L3 Windows 电脑操控 MCP 服务端 (18900)
│   ├── agy_sidecar_worker.py        # Antigravity 官方 Sidecar 派单守护进程
│   ├── sidecar.example.json         # Sidecar 配置模板
│   └── requirements.txt             # 宿主机电脑操控 Python 依赖
├── data/
│   ├── start_with_env.py            # 环境变量内存展开 + 记忆钩子 + 气象补丁加载
│   ├── console_server.py / .html    # 18080 本地可视化控制台
│   ├── anima_memory_provider.py     # Anima Brain 长期记忆对接模块
│   ├── nmc_weather_patch.py         # 中央气象台免 Key 实时与 7 天天气插件
│   └── start_all.ps1 / stop_all.ps1 # 启动/停止与冷内存回收脚本
├── anima-brain/                     # Anima Brain 长期记忆服务源码与 Dockerfile
└── skills/
    └── windows-pc-mcp-automation/   # 可独立安装的 Windows 高精度桌面操控 Agent Skill
```

---

## 快速上手

1. **配置系统环境变量**（PowerShell）：
   ```powershell
   [Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "你的DeepSeek密钥", "User")
   [Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "你的Gemini密钥", "User")
   ```
2. **安装宿主机电脑操控虚拟环境**：
   ```powershell
   cd pc-mcp
   python -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   ```
3. **准备本地模型目录**：
   - 将阿里 `SenseVoiceSmall/model.pt` 放入 `models/SenseVoiceSmall/model.pt`；
   - （可选）配置本地 `GPT-SoVITS` 目录及参考音频路径，或在 `18080` 控制台切换为免显卡的 `EdgeTTS`。
4. **（可选）启用 L3 反重力桥接**：
   - 将 `pc-mcp/sidecar.example.json` 复制到 `~/.gemini/config/sidecars/xiaozhi-agy-bridge/sidecar.json` 并填入实际路径，在 Antigravity 设置面板中开启 Sidecar 开关。
5. **双击 `启动或重启服务.bat`**，打开 http://localhost:18080 即可管理全部功能。

### 单独安装桌面操控 Agent Skill

```bash
npx skills add zou172866/xiaozhi-windows-pc-mcp
```
