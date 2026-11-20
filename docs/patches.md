<!-- AUTO-GENERATED FILE — DO NOT EDIT MANUALLY. -->
<!-- Source of truth: app/patches/registry.py (PATCH_CATALOG) -->
<!-- Regenerate: python scripts/generate_patches_doc.py -->
<!-- Verify in CI: python scripts/generate_patches_doc.py --check -->

# 启动补丁契约总览（`docs/patches.md`）

本文档由 `scripts/generate_patches_doc.py` 从 `app/patches/registry.py` 中的 `PATCH_CATALOG` 自动生成，
并与 `tests/container/test_upstream_contracts.py` 双向强校验（运行期实际注册补丁集合必须与 `PATCH_CATALOG` 1:1 匹配）。

## 概览与启动顺序

- **锁定上游镜像版本**：`xiaozhi-esp32-server` `0.9.8`（`docker-compose.yml` 锁定 `@sha256` digest）
- **补丁总数**：`20` 项（`security` 安全类 `4` 项 + `experience` 体验类 `16` 项）
- **唯一启动入口**：`app/boot.py`（模块导入零副作用，由 `install_all()` 显式挂载全部补丁 → `patches/contracts.py` 轻量契约自检写入注册表 → `assert_security_boot_gate()` 安全门禁 → 启动 `18080` 控制台与上游 `app.py`）
- **实时自检接口**：`GET http://127.0.0.1:18080/api/health`（返回每个补丁的状态、契约结果、早绑重绑列表与 `contract_self_check` 结果）

## 失败策略（安全 fail-closed / 体验 fail-soft）

- **`security` 类（`esp32_auth_ota`, `esp32_auth_vision`, `esp32_auth_ws`, `mcp_token_injection`）**：任意一项缺失、失败或签名契约不匹配时，`boot.py` 默认拒绝启动（`sys.exit(2)`）；仅当显式设置 `XIAOZHI_ALLOW_INSECURE_BOOT=1` 应急开关时放行，且 `/api/health` 持续返回 HTTP `503` 并在 `18080` 控制台顶栏挂出红色安全告警横幅。
- **`experience` 类**：签名契约不匹配时跳过替换原函数（记录 `skipped_contract`），提示词锚点缺失时逐条软跳过（记录 `partial`）；服务降级回退上游默认行为，`/api/health` 返回 HTTP `200` 且 `degraded` 列表清晰标注降级原因。

## 补丁明细表

| # | 补丁名 | 类别 | 实现模块 | 目标符号 | 签名 / 锚点契约 | 失败行为 | 对应测试 |
| :---: | :--- | :---: | :--- | :--- | :--- | :--- | :--- |
| 1 | `config_env_expand` | `experience` | `app/patches/auth.py` | `config.config_loader.read_config` | sync callable, >=1 positional arg; rebinds config.settings.read_config | fail-soft (records failed/skipped_contract, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 2 | `mcp_token_injection` | `security` | `app/patches/auth.py` | `core.providers.tools.server_mcp.mcp_manager.ServerMCPManager.load_config` | sync method accepting (self, ...) | fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 3 | `esp32_auth_ota` | `security` | `app/patches/auth.py` | `core.api.ota_handler.OTAHandler.handle_post` | async coroutine accepting (self, request) | fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 4 | `esp32_auth_ws` | `security` | `app/patches/auth.py` | `core.websocket_server.WebSocketServer.{_handle_auth,__init__} / ConnectionHandler.close` | async _handle_auth(self, websocket), sync __init__(self, config), async ConnectionHandler.close(self, ws=None) | fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 5 | `esp32_auth_vision` | `security` | `app/patches/auth.py` | `core.api.vision_handler.VisionHandler.{_verify_auth_token,handle_post}` | sync _verify_auth_token(self, request) and async handle_post(self, request) | fail-closed (sys.exit(2) unless XIAOZHI_ALLOW_INSECURE_BOOT=1 -> /api/health 503 + red banner) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 6 | `anima_memory_factory` | `experience` | `app/patches/memory_hooks.py` | `core.utils.memory.create_instance` | sync callable accepting >=2 positional args (class_name, *args, **kwargs) | fail-soft (falls back to upstream memory factory, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_batch3_anima_outbox.py` |
| 7 | `anima_dialogue_hooks` | `experience` | `app/patches/memory_hooks.py` | `core.utils.dialogue.Dialogue.{put,get_llm_dialogue_with_memory}` | sync methods put(self, message) and get_llm_dialogue_with_memory(self, memory_str, ...) | fail-soft (skips mount on contract mismatch, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_batch3_pipeline_vad.py` |
| 8 | `anima_turn_flush` | `experience` | `app/patches/memory_hooks.py` | `core.connection.ConnectionHandler.{chat,_handle_function_result,close}` | sync chat / _handle_function_result and async close(self, ...) | fail-soft (falls back to debounced loop flush, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_batch3_pipeline_vad.py` |
| 9 | `nmc_weather` | `experience` | `app/patches/weather.py` | `plugins_func.functions.get_weather.get_weather` | registered ToolType.SYSTEM_CTL plugin callable accepting (conn, location, lang) | fail-soft (retains upstream weather plugin, marks /api/health degraded) | `tests/container/test_batch3_pipeline_vad.py`<br>`tests/container/test_upstream_contracts.py` |
| 10 | `pc_audio_tts` | `experience` | `app/patches/pc_audio.py` | `core.providers.tts.base.TTSProviderBase.to_tts_stream` | sync method accepting (self, text, opus_handler, ...); must install before mei_emotion_and_prosody | fail-soft (keeps ESP32 speaker path, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_batch3_pipeline_vad.py` |
| 11 | `mei_prompt_anchors` | `experience` | `app/patches/emotion/prompt.py` | `agent-base-prompt.txt (4 anchors) via PromptManager.build_enhanced_prompt` | 4 prompt-harmony anchor strings present in upstream agent-base-prompt.txt | fail-soft per anchor (skips missing anchor with one-shot warning, records partial -> /api/health 200 degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 12 | `mei_emotion_and_prosody` | `experience` | `app/patches/emotion/tts_route.py` | `TTSProviderBase.{tts_text_priority_thread,to_tts_stream} / GPT_SOVITS_V2 / ConnectionHandler._clean_response_garbage / sendAudioHandle.send_tts_message` | sync TTS/clean hooks + async send_tts_message(conn, state, text); rebinds early imports | fail-soft (falls back to upstream TTS/prosody, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_batch3_pipeline_vad.py` |
| 13 | `pc_audio_playback` | `experience` | `app/patches/pc_audio.py` | `core.handle.sendAudioHandle.send_tts_message / helloHandle / abortHandle` | async send_tts_message & handleAbortMessage; rebinds listenMessageHandler & receiveAudioHandle | fail-soft (falls back to ESP32 playback, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_batch3_pipeline_vad.py` |
| 14 | `mei_wakeup_responses` | `experience` | `app/patches/emotion/wakeup.py` | `core.handle.helloHandle.{checkWakeupWords,handleHelloMessage} / intentHandler.checkWakeupWords` | async checkWakeupWords(conn, text) & handleHelloMessage(conn, msg_json); must install after pc_audio_playback | fail-soft (falls back to upstream wakeup replies, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 15 | `listen_interrupt_handler` | `experience` | `app/patches/emotion/wakeup.py` | `core.handle.textHandler.listenMessageHandler.ListenTextMessageHandler.handle` | async method handle(self, conn, msg_json) | fail-soft (falls back to upstream listen handler, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 16 | `idle_farewell` | `experience` | `app/patches/emotion/farewell.py` | `core.handle.receiveAudioHandle.no_voice_close_connect` | async coroutine no_voice_close_connect(conn, have_voice); rebinds callers | fail-soft (falls back to upstream idle timeout close, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 17 | `reply_length_llm` | `experience` | `app/patches/reply_length.py` | `core.providers.llm.openai.openai.LLMProvider.{response,response_with_functions}` | sync generators response(self, session_id, dialogue, ...) & response_with_functions(...) | fail-soft (uses upstream LLM max_tokens & messages, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 18 | `llm_prefix_cache` | `experience` | `app/turn_context_router.py` | `core.providers.llm.openai.openai.LLMProvider.{__init__,response,response_with_functions}` | sync __init__(self, config) + generators response & response_with_functions; hot-rollback via turn_context_mode=legacy | fail-soft (keeps legacy system prompt layout, marks /api/health degraded) | `tests/container/test_mei_emotion_and_prosody.py`<br>`tests/container/test_upstream_contracts.py` |
| 19 | `vad_speculative` | `experience` | `app/vad_speculative_router.py` | `ASRProviderBase.{receive_audio,handle_voice_stop,speech_to_text_wrapper} / ConnectionHandler.reset_audio_states / receiveAudioHandle.handleAudioMessage` | async receive_audio/handle_voice_stop/speech_to_text_wrapper/handleAudioMessage + sync reset_audio_states; hot-toggleable to pure 1100ms VAD | fail-soft (skips mount on contract mismatch -> pure 1100ms VAD, marks /api/health degraded) | `tests/container/test_batch3_pipeline_vad.py`<br>`tests/container/test_batch3_console.py`<br>`tests/container/test_upstream_contracts.py` |
| 20 | `sherpa_async_asr` | `experience` | `app/patches/asr.py` | `core.providers.asr.sherpa_onnx_local.ASRProvider.speech_to_text` | async coroutine speech_to_text(self, opus_data, session_id, artifacts=None); offloads decode_stream to dedicated 1-thread asr executor | fail-soft (runs upstream sync decode, marks /api/health degraded) | `tests/container/test_batch3_pipeline_vad.py`<br>`tests/container/test_upstream_contracts.py` |

## 升级上游镜像或增删补丁后的必跑命令

```bash
# 1. 离线跑上游镜像契约测试（临时 docker run --rm，不依赖正在运行的容器）
python scripts/run_tests.py --layer contract

# 2. 若在 app/patches/registry.py 中增改了 PATCH_CATALOG，重新生成并校验本文档
python scripts/generate_patches_doc.py
python scripts/generate_patches_doc.py --check
```
