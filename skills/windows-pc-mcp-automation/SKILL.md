---
name: windows-pc-mcp-automation
description: >-
  在 Windows 高分屏环境下构建高精度桌面/浏览器 GUI 操控 MCP 服务、后台静默交互进程与 Antigravity Sidecar 桥接的实战指南。
  当需要实现精确鼠标视觉/UIA 定位点击、解决后台打开网页/软件窗口不自动弹出置顶、修复 Windows 125%/150%/200% DPI 缩放坐标错位、从沙盒启动桌面守护进程或回收 PyTorch/WSL2 闲置物理内存时使用。
---

# Windows 高精度桌面操控与守护进程工程指南

解决 Windows 桌面 Agent 最常见的四大痛点：**高分屏点击偏位、后台打开窗口不置顶、沙盒桌面截屏失败、本地模型吃满系统内存**。

## 1. 锁死 1:1 真实物理像素坐标系（防高分屏错位）

在 `200%` 等高分屏（如 `2560×1440 @ 192 DPI`）上，未声明 DPI 感知的进程会让 `pyautogui` 处于逻辑像素（`1280×720`），与物理截图坐标差整整一倍。

- **必须在导入 `pyautogui`、`PIL.ImageGrab`、`uiautomation` 之前**，在入口脚本最顶部执行：
  ```python
  import ctypes
  try:
      # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
      ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
  except Exception:
      ctypes.windll.shcore.SetProcessDpiAwareness(2)
  ```
- **严禁让主对话大模型看纯文字描述盲猜 `(x, y)`**：将定位封装在 `click(element_name="...")` 工具内部，并在工具 docstring 中禁止主对话模型先调看图工具自己估算坐标。

## 2. 三阶高精度鼠标定位流水线（UIA + SoM + 局部放大精修）

1. **第一阶：18 层 UIA 原生矩形直点（0.15s 零误差）**
   - 用 `uiautomation.WalkControl(fg, maxDepth=18)`（设约 `2800` 节点上限保速度）穿透窗口；
   - 对非方位词目标（如明确按钮名），直接取 `BoundingRectangle` 物理中心点击。
   - 注意：Chromium/Edge 默认关闭网页 DOM 无障碍树，顶部常只暴露标签栏/书签栏控件，网页正文元素需走第二阶视觉定位。
2. **第二阶：Set-of-Mark 红框数字角标 + 防颠倒 `0~1000` 归一化框**
   - 在 1:1 物理截图上为已检出的 UIA 控件绘制红底白字编号 `[1]..[N]`；
   - 调用低延迟多模态视觉接口（如 `deepseek-flash` 配 `"thinking": {"type": "disabled"}` + `response_format={"type": "json_object"}`），**强制使用显式命名键 `x_min, y_min, x_max, y_max`（`0~1000` 归一化）**，杜绝不同模型混淆 `[ymin, xmin]` 与 `[xmin, ymin]` 顺序；
   - 若返回有效 `mark_id` 且与视觉框距离合理，优先吸附至 UIA 原生中心。
3. **第三阶：小目标 1:1 原图裁剪二次精修（GUI-Lens 模式）**
   - 当未命中 UIA 角标且视觉框较小（如宽 `< 200px` 或高 `< 110px`）时，以粗定位中心在原图裁剪 `768×512` 无缩放高清切片二次定位，将像素误差压至 `< 10px`；
   - 将最终落点绘成绿十字准星保存至单张覆写核验图（如 `last_som_grounding.jpg`）以便目视排错；点击前先 `pyautogui.moveTo(cx, cy, duration=0.12)` 触发网页 `:hover` 态再点击。

## 3. 破除 Windows `ForegroundLockTimeout` 强制弹出窗口

后台守护进程直接调 `webbrowser.open()` 只会在任务栏闪烁而不会跳到最前。必须：
1. **64 位 `ctypes.wintypes` 类型声明**：`SetWindowPos` 的 `HWND_TOPMOST` / `HWND_NOTOPMOST` 必须用 `wintypes.HWND(-1)` / `wintypes.HWND(-2)`（在 64 位 Python 中裸传 `-1` 会被当成 32 位整型导致失效）。
2. **安全唤醒与置顶翻转**：
   - 若 `IsIconic(hwnd)` 为真才调 `ShowWindow(hwnd, SW_RESTORE)`，否则调 `SW_SHOW`（避免把已最大化的浏览器缩回小窗）；
   - 通过 `AttachThreadInput` 临时挂载当前前台窗口线程输入队列，执行 `SetWindowPos(HWND_TOPMOST) -> SetWindowPos(HWND_NOTOPMOST) -> BringWindowToTop -> SetForegroundWindow`（若仍未获焦点，辅以一次无副作用未分配虚拟键 `0xFF` 脉冲解锁前台权限）。
3. **浏览器精准识别**：通过 `DwmGetWindowAttribute(DWMWA_CLOAKED=14)` 剔除隐藏 UWP 幽灵窗，并按**进程名白名单**（`msedge.exe` / `chrome.exe` 等）筛选浏览器，绝不只按 `Chrome_WidgetWin_1` 类名匹配以免误抓 Electron 桌面端。

## 4. 沙盒外启动守护进程与无损内存回收

- **挂载真实交互桌面且无黑框**：在 Agent 受限终端（非默认桌面）中启动需截屏/控窗的宿主机常驻服务时，使用 WMI：
  ```powershell
  $si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; ProcessStartupInformation = $si }
  ```
- **PyTorch GPU 服务与 WSL2 内存瘦身**：
  - PyTorch 模型加载进显卡后，CPU 工作集常残留数 GB 冷缓存页；待端口就绪后对进程句柄调用一次 `psapi.dll!EmptyWorkingSet`，可在零推理降速下立省 `3GB+` 物理内存；
  - `%USERPROFILE%\.wslconfig` 配置 `autoMemoryReclaim=gradual` 与 `sparseVhd=true`，防止 `vmmem` 吞占 Linux PageCache 不归还。
