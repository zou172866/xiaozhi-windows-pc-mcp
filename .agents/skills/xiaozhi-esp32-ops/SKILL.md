---
name: xiaozhi-esp32-ops
description: >-
  小智 ESP32-S3（xiaozhi-esp32 v2.x）免编译源码定制离线中文唤醒词（MultiNet7 assets.bin 打包、
  灵敏度 threshold 微调、NVS 免重配网备份刷写）以及 Anima Brain 长期记忆与情绪系统深度排障手册。
  当用户要求更换 ESP32 唤醒词、调整唤醒灵敏度、刷写/升级小智固件，或排查 Anima Brain 记忆不生效、
  向量检索/长期总结卡死、关系停滞疏离、重复追问过期旧事时使用。
---

# 小智 ESP32-S3 固件唤醒词定制与 Anima Brain 记忆排障指南

## 一、免编译源码定制 ESP32-S3 离线中文唤醒词（`v2.x` + `MultiNet7`）

### 1. 适用前提与必查项
1. **芯片与 Flash 核验**：用 `esptool --chip esp32s3 --port <COMx> flash-id` 确认具备 **16MB Flash + 8MB PSRAM**（`partitions/v2/16m.csv` 的 `assets` 分区位于 `0x800000`，大小 `8 MB`）。
2. **屏幕尺寸绝不盲猜**：`bread-compact-wifi` 官方预编译包区分 **`128×32`（0.91 寸细长屏）** 与 **`128×64`（0.96 寸方屏）**，刷主固件前必须与用户确认屏幕规格。
3. **刷前强备份（防丢 Wi-Fi 与本地 OTA）**：
   ```powershell
   esptool --chip esp32s3 --port <COMx> --baud 460800 read-flash 0x9000 0x4000 data\nvs_local_backup.bin
   esptool --chip esp32s3 --port <COMx> --baud 460800 read-flash 0x0 0x10000 data\esp32_boot_pt_nvs_backup.bin
   ```

### 2. 打包自定义 `assets.bin` 的 4 个关键不变量
1. **模型版本与 `fst` 必配**：从乐鑫组件库下载与固件 `idf_component.yml` 匹配的 `espressif/esp-sr`（如 `2.4.7`），在 `srmodels.bin` 中**必须同时打包 `mn7_cn`（或 `mn6_cn`）与 `model/multinet_model/fst/` 目录**；漏打 `fst/` 会直接报错 `no (null)/fst/commands_cn.txt ... cannot build fst`。
2. **拼音校验**：中文唤醒词使用无声调小写空格分隔拼音（如 `ni hao ya yi`），打包前核对每个音节前加 SentencePiece 下划线（如 `▁ni`）存在于 `mn7_cn/vocab`。
3. **保留原字体 + 正确打包格式**：保留官方默认 `assets` 中的 `font_noto_sans_common_14_1.bin` 与 `text_font_meta`，在 `index.json` 写入 `multinet_model`（`language: "cn"`, `duration: 3000`, `threshold: 0.2`, `commands: [{"command": "...", "text": "...", "action": "wake"}]`）；按 `esp_mmap_assets` 格式封装（12 字节头 `count, sum(payload)&0xFFFF, len` + 44 字节/项文件表 + 每项前导 `0x5A 0x5A`）。
4. **免重配网刷写与 10 秒热调灵敏度**：
   - 首次从 v1 升 v2 时，从 `merged-binary.bin` 分片拆出 `bootloader` (`0x0`)、`partition-table` (`0x8000`)、`ota_data` (`0xd000`)、`app` (`0x20000`)，配合原位备份的 `0x9000` NVS 与 `0x800000` 自定义 `assets.bin` 一次写入；
   - 后续仅微调唤醒灵敏度（`threshold` 越小越灵敏，范围 `0.05~0.50`）或增改词条时，**只需 10 秒单刷 `0x800000` 的 `assets.bin`**，不碰主固件与 NVS。

### 3. 串口与服务端双端验收
- 复位抓串口 7 秒日志，必见：`Successfully load srmodels` → `CustomWakeWord: Command: ...` → `1 active speech commands` → `Initialized FD AFE, detector: MultiNet` → `Got IP` 与本地 OTA 握手成功。
- 服务端同步在 `data/.config.yaml` 的 `wakeup_words` 加入新唤醒词与常见同音词（如 `你好芽衣` / `你好牙医`），保留 `嘿你好呀` / `你好小智` 兜底；若定制唤醒秒回短 WAV（GPT-SoVITS V4），用 RMS 能量阈值裁剪前导软气声（留 ~40ms 起音缓冲）以消除开头喘气延迟。

---

## 二、Anima Brain 长期记忆深度排障手册

当用户反馈“记忆不对劲 / 记不住 / 突然冷淡 / 一见面复读旧事”时，按以下顺序逐层核验（先抓现场证据再修）：

1. **直接打印真实发给 LLM 的 `<memory>` 召回全文**：不要只看日志里的 `事实=N`，在 `xiaozhi-esp32-server` 内调 `MemoryProvider.query_memory(...)` 亲眼检查注入文本是否存在：
   - 关系提示段写着 `barely acquainted. Keep warmth light and unassuming`（熟悉度卡在滞回升档阈值 `0.285` 以下，检查抽取提示词是否漏了 `disclosure` 评分）；
   - 未标日期或已过期的 `open_loops` 诱发大模型每轮机械复读；
   - 跨轮 `recent_turns` 截断过短或混入旧称呼原话。
2. **核查 `brain.sqlite3` 的后台整理健康度**：
   - 检查 `vectors`、`evidence_embeddings`、`documents` 是否长期为 `0`，以及 `memory_jobs` 是否存在卡在 `state='review'` 的任务；
   - 若出现 `provider_or_validation_failed_outcome_unknown`，在容器内直接调用 `runtime.index` / `runtime.reconcile` 捕获真实堆栈——重点警惕 OpenAI 兼容代理（如 Gemini `/embeddings`）省略条目 `index` 字段触发 `media.py` `KeyError` 或偶发 `503` 永久锁死队列。
3. **安全修复原则**：
   - 修改 `brain.sqlite3` 前**必须先停 `xiaozhi-anima-brain` 并备份原库**；
   - 涉及修改用户既有关系数值或关闭/修正具体私人记忆条目时，先向用户列出明细确认后再落盘。
