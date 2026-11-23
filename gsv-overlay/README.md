# GPT-SoVITS Windows 免 MSVC 一键环境配置与小智补丁套件 (`gsv-overlay/`)

本目录随 `xiaozhi-server` 一起发布，用于在新 Windows 电脑上为宿主机 GPU 语音合成引擎（`127.0.0.1:19880`，支持 **V4 雷电芽衣** 与 **V5-Turbo 罗小黑**）一键配齐免 Visual Studio / MSVC 编译的二进制依赖并注入小智专用服务端补丁。

## 解决了哪些上游痛点？

1. **免装几 GB 的 VS C++ Build Tools（报告一 P0-1）**：
   - 自动将 Windows 无预编译轮子的 `opencc` 替换为纯 Python 兼容包 `opencc-python-reimplemented`；
   - 自动补齐上游 `requirements.txt` 漏声明的 `resampy`；
   - 将 `chinese.py` / `chinese2.py` / `tone_sandhi.py` 改为优先 `jieba_fast`、缺省平滑回退 `jieba`（并在 `.venv` 写入兼容垫片），剔除中英场景不需要的 `pyopenjtalk` 源码编译阻塞。
2. **补齐 `19880` 启动参数、`/prime` 预热与日志轮转（报告二 P0-1）**：
   - 幂等注入 `--log_file` / `--log_max_bytes` / `--log_backup_count`（`5 MB × 3` 轮转）、`GET/POST /prime` 7 情绪参考特征预热接口，以及 `GSV_IDLE_TRIM_*` 闲置冷工作集回收。
3. **根治 CUDA Graph 内存不足返回 HTTP 200 全零废音（报告二 P0-2）**：
   - 对 `TTS.py` 的 `t2s_accel`（`torch_static_cuda_graph`）单独加异常捕获：一旦构建静态图内存不足，当场禁用 CUDA Graph 并自动回退 `infer_panel` 正常输出 **48kHz** 音频，且绝不把 `1.00s / 16kHz` 全零占位帧当成 HTTP `200` 返回。

## 使用方法

### 方式一：新克隆 `GPT-SoVITS` 后一键建虚拟环境 + 装依赖 + 打补丁（推荐）

将上游 `GPT-SoVITS`（含支持 `v5turbo` 的分支）克隆或解压到与 `xiaozhi-server` 同级的 `gpt-sovits-studio` 目录（或任意自定义目录），在 PowerShell 中执行：

```powershell
# 默认处理同级 ..\gpt-sovits-studio（自动下载基础预训练底模与 G2PW 多音字模型）
powershell -NoProfile -ExecutionPolicy Bypass -File .\gsv-overlay\setup_gsv_windows.ps1

# 指定自定义目录与国内 PyPI 镜像源（已下好底模可加 -SkipModels）
powershell -NoProfile -ExecutionPolicy Bypass -File .\gsv-overlay\setup_gsv_windows.ps1 `
  -GsvDir "D:\AI\gpt-sovits-studio" `
  -PypiMirror "https://pypi.tuna.tsinghua.edu.cn/simple"
```

### 方式二：已有 `.venv` 环境，仅增量打小智补丁

```powershell
python .\gsv-overlay\apply_xiaozhi_gsv_patch.py --gsv-dir "..\gpt-sovits-studio" --install-venv-shims
```

检查补丁是否已完整生效（已打补丁返回退出码 `0`）：

```powershell
python .\gsv-overlay\apply_xiaozhi_gsv_patch.py --gsv-dir "..\gpt-sovits-studio" --check
```
