import asyncio
import json
import os
import re
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import edge_tts
import httpx
import yaml

import mei_emotion_router
import pc_audio_router
import reply_length_router

ROOT_DIR = "/opt/xiaozhi-esp32-server"
DATA_DIR = os.path.join(ROOT_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, ".config.yaml")
HTML_PATH = os.path.join(DATA_DIR, "console.html")
HISTORY_PATH = os.path.join(DATA_DIR, "chat_history.json")
MODE_PATH = os.path.join(DATA_DIR, "pc_control_mode.json")
AGY_HB_PATH = os.path.join(DATA_DIR, "agy_tasks", "worker_heartbeat.json")
LOG_PATH = os.path.join(ROOT_DIR, "tmp", "server.log")

CONSOLE_PORT = int(os.environ.get("CONSOLE_PORT", "18080"))
GSV_Host = "http://host.docker.internal:19880"
GSV_INFER_YAML = os.path.join(DATA_DIR, "tts_infer_xiaozhi.yaml")

GSV_PROFILES = {
    "__gsv_raiden_mei__": {
        "version": "v4",
        "gpt_weights": "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/v4/芽衣/芽衣-e10.ckpt",
        "sovits_weights": "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/v4/芽衣/芽衣_e10_s430_l32.pth",
        "block": {
            "type": "gpt_sovits_v2",
            "url": f"{GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/mei_default_ref.wav",
            "aux_ref_audio_paths": [],
            "prompt_text": "今夜月色很美，呵...适合与朋友小聚一番。",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "split_bucket": True,
            "return_fragment": False,
            "speed_factor": 1.0,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
        },
    },
    "__gsv_xiaozhi_tw__": {
        "version": "v2ProPlus",
        "gpt_weights": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "sovits_weights": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
        "block": {
            "type": "gpt_sovits_v2",
            "url": f"{GSV_Host}/tts",
            "output_dir": "tmp/",
            "text_lang": "zh",
            "ref_audio_path": "e:/故事/1/xiaozhi-server/data/xiaozhi_taiwan_ref.wav",
            "aux_ref_audio_paths": ["e:/故事/1/xiaozhi-server/data/xiaozhi_taiwan_aux.wav"],
            "prompt_text": "哈啰啊，我是小智啦，声音好听的台湾女孩一枚，超开心认识你耶，最近在忙啥？",
            "prompt_lang": "zh",
            "top_k": 12,
            "top_p": 1,
            "temperature": 0.98,
            "text_split_method": "cut0",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "split_bucket": True,
            "return_fragment": False,
            "speed_factor": 1.04,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
        },
    },
}
_gsv_weights_lock = threading.RLock()
_active_gsv_loaded = None


def _detect_boot_gsv_voice() -> str | None:
    if os.path.exists(GSV_INFER_YAML):
        try:
            with open(GSV_INFER_YAML, "r", encoding="utf-8") as rf:
                boot_cfg = yaml.safe_load(rf) or {}
            t2s = str((boot_cfg.get("custom") or {}).get("t2s_weights_path") or "")
            for vid, prof in GSV_PROFILES.items():
                if prof["gpt_weights"] == t2s:
                    return vid
        except Exception:
            pass
    return None


def _get_configured_gsv_voice() -> str | None:
    try:
        cfg = _read_raw_yaml()
        selected = cfg.get("selected_module", {})
        if selected.get("TTS") != "GPT_SOVITS_V2":
            return None
        ref_p = str((cfg.get("TTS", {}).get("GPT_SOVITS_V2") or {}).get("ref_audio_path") or "")
        return "__gsv_raiden_mei__" if "raiden_mei" in ref_p else "__gsv_xiaozhi_tw__"
    except Exception:
        return None


def _ensure_gsv_weights(voice_id: str, persist_boot_yaml: bool = False):
    global _active_gsv_loaded
    prof = GSV_PROFILES.get(voice_id)
    if not prof:
        return
    with _gsv_weights_lock:
        if _active_gsv_loaded is None:
            _active_gsv_loaded = _detect_boot_gsv_voice()
        if _active_gsv_loaded != voice_id:
            httpx.get(f"{GSV_Host}/set_gpt_weights", params={"weights_path": prof["gpt_weights"]}, timeout=25.0)
            httpx.get(f"{GSV_Host}/set_sovits_weights", params={"weights_path": prof["sovits_weights"]}, timeout=30.0)
            _active_gsv_loaded = voice_id
        if persist_boot_yaml:
            boot_cfg = {}
            if os.path.exists(GSV_INFER_YAML):
                try:
                    with open(GSV_INFER_YAML, "r", encoding="utf-8") as rf:
                        boot_cfg = yaml.safe_load(rf) or {}
                except Exception:
                    boot_cfg = {}
            boot_cfg["custom"] = {
                "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
                "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
                "device": "cuda",
                "is_half": True,
                "t2s_weights_path": prof["gpt_weights"],
                "version": prof["version"],
                "vits_weights_path": prof["sovits_weights"],
            }
            with open(GSV_INFER_YAML, "w", encoding="utf-8") as f:
                yaml.safe_dump(boot_cfg, f, allow_unicode=True, sort_keys=False)


_state_lock = threading.Lock()
_chat_messages = []
_raw_logs = deque(maxlen=150)
_device_info = {
    "device_id": "cc:68:c7:a8:19:dc",
    "last_seen": "",
    "activity": "idle",
    "server_ready": False,
}
_current_ai_turn_open = False
_log_offset = 0


def _load_history():
    global _chat_messages
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
                if isinstance(items, list):
                    _chat_messages = items[-100:]
        except Exception:
            _chat_messages = []


def _save_history():
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(_chat_messages[-100:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _read_pc_mode() -> str:
    try:
        if os.path.exists(MODE_PATH):
            with open(MODE_PATH, "r", encoding="utf-8") as f:
                m = json.load(f).get("mode", "safe")
                if m in ("off", "safe", "full"):
                    return m
    except Exception:
        pass
    return "safe"


def _write_pc_mode(mode: str):
    os.makedirs(os.path.dirname(MODE_PATH), exist_ok=True)
    with open(MODE_PATH, "w", encoding="utf-8") as f:
        json.dump({"mode": mode}, f, ensure_ascii=False, indent=2)


def _check_agy_bridge_online() -> bool:
    try:
        if os.path.exists(AGY_HB_PATH):
            with open(AGY_HB_PATH, "r", encoding="utf-8") as f:
                hb = json.load(f)
            return (time.time() - float(hb.get("ts", 0))) < 15.0
    except Exception:
        pass
    return False


_RE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
_RE_OTA_DEV = re.compile(r"OTA请求设备ID:\s*([0-9a-fA-F:]+)")
_RE_LISTEN_START = re.compile(r'收到listen消息：.*"state"\s*:\s*"start"')
_RE_USER_MSG = re.compile(r"大模型收到用户消息:\s*(.+)$")
_RE_AI_SENTENCE = re.compile(r"发送音频消息:\s*SentenceType\.(\w+),\s*(.+)$")


def _join_cn_clauses(prev: str, nxt: str) -> str:
    if not prev:
        return nxt
    if prev[-1] in "，。！？；：,.!?;:~～…":
        return prev + nxt
    return prev + "，" + nxt


def _process_log_line(line: str):
    global _current_ai_turn_open
    s = line.strip()
    if not s:
        return

    with _state_lock:
        _raw_logs.append(s)

        m_time = _RE_TIME.match(s)
        ts_short = m_time.group(1)[11:19] if m_time else time.strftime("%H:%M:%S")

        if "ASR模块初始化完成" in s or "Websocket地址是" in s:
            _device_info["server_ready"] = True

        m_dev = _RE_OTA_DEV.search(s)
        if m_dev:
            _device_info["device_id"] = m_dev.group(1)
            _device_info["last_seen"] = ts_short

        if _RE_LISTEN_START.search(s):
            _device_info["activity"] = "listening"
            _device_info["last_seen"] = ts_short
            _current_ai_turn_open = False
            return

        m_user = _RE_USER_MSG.search(s)
        if m_user:
            text = m_user.group(1).strip()
            _device_info["activity"] = "speaking"
            _device_info["last_seen"] = ts_short
            _current_ai_turn_open = False
            _chat_messages.append({"role": "user", "text": text, "time": ts_short})
            _save_history()
            return

        m_ai = _RE_AI_SENTENCE.search(s)
        if m_ai:
            stype, raw_content = m_ai.group(1), m_ai.group(2).strip()
            content = (
                "None"
                if raw_content == "None"
                else mei_emotion_router.strip_emotion_tags(raw_content).strip()
            )
            _device_info["last_seen"] = ts_short
            if stype == "LAST" or content == "None":
                _current_ai_turn_open = False
                _device_info["activity"] = "idle"
                _save_history()
            elif content:
                _device_info["activity"] = "speaking"
                if _current_ai_turn_open and _chat_messages and _chat_messages[-1]["role"] == "assistant":
                    _chat_messages[-1]["text"] = _join_cn_clauses(_chat_messages[-1]["text"], content)
                else:
                    _chat_messages.append({"role": "assistant", "text": content, "time": ts_short})
                    _current_ai_turn_open = True
            return


def _log_watcher_loop():
    global _log_offset
    _load_history()
    last_inode = None
    while True:
        try:
            if os.path.exists(LOG_PATH):
                st = os.stat(LOG_PATH)
                if last_inode != st.st_ino or st.st_size < _log_offset:
                    _log_offset = 0
                    last_inode = st.st_ino
                if st.st_size > _log_offset:
                    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(_log_offset)
                        chunk = f.read()
                        _log_offset = f.tell()
                    for ln in chunk.splitlines():
                        _process_log_line(ln)
        except Exception:
            pass
        time.sleep(0.4)


def _read_raw_yaml() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


ANIMA_HOST = "http://anima-brain:18787"
ANIMA_TOKEN_PATH = os.path.join(DATA_DIR, "anima", "studio_token.txt")


def _infer_character_name(prompt: str) -> str:
    p = (prompt or "").strip()
    if "雷电芽衣" in p:
        return "雷电芽衣"
    if "芽衣" in p:
        return "芽衣"
    m = re.search(r"你是(?:《[^》]+》中的)?([\u4e00-\u9fa5A-Za-z0-9]{2,8}?)(?:[，,。！!、\s]|$)", p)
    if m:
        cand = m.group(1).strip()
        if cand and not cand.startswith(("一个", "一名", "一位", "住在")):
            return cand
    return "小智"


def _sync_persona_to_anima(prompt: str):
    """将 18080 控制台的角色姓名与提示词自动同步至 18787 Anima Brain 数据库，确保双端一致。"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt or not os.path.isfile(ANIMA_TOKEN_PATH):
        return
    try:
        with open(ANIMA_TOKEN_PATH, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            return
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            boot = client.get(f"{ANIMA_HOST}/api/bootstrap", headers=headers)
            if boot.status_code != 200:
                return
            chars = boot.json().get("characters") or []
            target_spec = next((c for c in chars if c.get("id") == "xiaozhi"), None)
            if not target_spec:
                return
            new_name = _infer_character_name(clean_prompt)
            anima_persona = mei_emotion_router.strip_voice_tag_instruction_for_anima(clean_prompt)
            if target_spec.get("name") == new_name and (target_spec.get("persona") or "").strip() == anima_persona:
                return
            updated = dict(target_spec)
            updated["name"] = new_name
            updated["persona"] = anima_persona
            r = client.post(f"{ANIMA_HOST}/api/update_character", headers=headers, json={"spec": updated})
            if r.status_code == 200:
                print(f"[WebConsole] 已自动同步角色设定至 Anima Brain: name={new_name}", flush=True)
    except Exception as exc:
        print(f"[WebConsole] 同步 Anima Brain 设定跳过: {exc}", flush=True)


def _write_updated_yaml(prompt: str, llm_provider: str, model_name: str, voice: str):
    cfg = _read_raw_yaml()
    cfg["prompt"] = prompt.strip() + "\n"

    selected = cfg.setdefault("selected_module", {})
    llm_section = cfg.setdefault("LLM", {})

    # Ensure both DeepSeekLLM and GeminiOpenAI blocks always remain intact
    ds_cfg = llm_section.setdefault("DeepSeekLLM", {})
    ds_cfg["type"] = "openai"
    ds_cfg["base_url"] = "https://api.deepseek.com"
    ds_cfg.setdefault("model_name", "deepseek-flash")
    ds_cfg["api_key"] = "${DEEPSEEK_API_KEY}"

    gem_cfg = llm_section.setdefault("GeminiOpenAI", {})
    gem_cfg["type"] = "openai"
    gem_cfg["base_url"] = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gem_cfg.setdefault("model_name", "gemini-3.5-flash-lite")
    gem_cfg["api_key"] = "${GEMINI_API_KEY}"

    if llm_provider == "GeminiOpenAI":
        selected["LLM"] = "GeminiOpenAI"
        gem_cfg["model_name"] = model_name.strip() or "gemini-3.5-flash-lite"
    else:
        selected["LLM"] = "DeepSeekLLM"
        ds_cfg["model_name"] = model_name.strip() or "deepseek-flash"

    tts_section = cfg.setdefault("TTS", {})
    v_id = voice.strip()
    if v_id in GSV_PROFILES:
        _ensure_gsv_weights(v_id, persist_boot_yaml=True)
        selected["TTS"] = "GPT_SOVITS_V2"
        tts_section["GPT_SOVITS_V2"] = dict(GSV_PROFILES[v_id]["block"])
    else:
        selected["TTS"] = "EdgeTTS"
        tts_cfg = tts_section.setdefault("EdgeTTS", {})
        tts_cfg.setdefault("type", "edge")
        tts_cfg["voice"] = v_id
        tts_cfg.setdefault("output_dir", "tmp/")

    header = (
        "# =====================================================================\n"
        "# 小智 ESP32 本地服务端精简配置（由本地 Web 控制台管理）\n"
        "# 自动读取 DEEPSEEK_API_KEY 与 GEMINI_API_KEY 环境变量，不落盘明文密钥\n"
        "# =====================================================================\n\n"
    )
    body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(header + body)
    threading.Thread(target=_sync_persona_to_anima, args=(prompt,), daemon=True).start()


async def _synthesize_mp3(text: str, voice: str) -> bytes:
    comm = edge_tts.Communicate(text=text, voice=voice)
    chunks = bytearray()
    async for item in comm.stream():
        if item.get("type") == "audio":
            chunks.extend(item["data"])
    return bytes(chunks)


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, payload: dict, status=HTTPStatus.OK):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            if not os.path.exists(HTML_PATH):
                self._send_json({"error": "console.html not found"}, HTTPStatus.NOT_FOUND)
                return
            with open(HTML_PATH, "rb") as f:
                html_bytes = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        if parsed.path == "/api/state":
            cfg = _read_raw_yaml()
            selected = cfg.get("selected_module", {})
            llm_key = selected.get("LLM", "DeepSeekLLM")
            tts_key = selected.get("TTS", "GPT_SOVITS_V2")
            llm_cfg = cfg.get("LLM", {}).get(llm_key, {})
            tts_cfg = cfg.get("TTS", {}).get(tts_key, {})
            srv_cfg = cfg.get("server", {})
            ws_url = srv_cfg.get("websocket", "ws://192.168.31.200:18000/xiaozhi/v1/")
            ota_url = ws_url.replace("ws://", "http://").replace(":18000/xiaozhi/v1/", ":18003/xiaozhi/ota/")

            ds_env = os.environ.get("DEEPSEEK_API_KEY", "")
            gem_env = os.environ.get("GEMINI_API_KEY", "")
            ds_ok = bool(ds_env and len(ds_env) > 10 and "请在这里" not in ds_env)
            gem_ok = bool(gem_env and len(gem_env) > 10 and "请在这里" not in gem_env)

            if tts_key == "GPT_SOVITS_V2":
                ref_p = str(tts_cfg.get("ref_audio_path", ""))
                active_voice = "__gsv_raiden_mei__" if "raiden_mei" in ref_p else "__gsv_xiaozhi_tw__"
            else:
                active_voice = tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural")

            with _state_lock:
                dev = dict(_device_info)
                msgs = list(_chat_messages[-60:])
                logs = list(_raw_logs)[-100:]

            audio_cfg = pc_audio_router.read_audio_output_config()
            reply_len_cfg = reply_length_router.read_reply_length_config()

            self._send_json(
                {
                    "config": {
                        "prompt": (cfg.get("prompt") or "").strip(),
                        "llm_provider": llm_key,
                        "model_name": llm_cfg.get(
                            "model_name",
                            "deepseek-flash" if llm_key == "DeepSeekLLM" else "gemini-3.5-flash-lite",
                        ),
                        "voice": active_voice,
                        "websocket": ws_url,
                        "ota_url": ota_url,
                        "deepseek_key_ok": ds_ok,
                        "gemini_key_ok": gem_ok,
                        "pc_mode": _read_pc_mode(),
                        "audio_output": audio_cfg["target"],
                        "pc_audio_volume": audio_cfg["volume"],
                        "pc_audio_online": pc_audio_router.is_pc_audio_bridge_online(),
                        "reply_length": reply_len_cfg,
                        "agy_bridge_online": _check_agy_bridge_online(),
                        "mei_emotions": [
                            {"id": k, "label": v.get("label_zh", k)}
                            for k, v in mei_emotion_router.load_mei_emotions().items()
                        ],
                    },
                    "device": dev,
                    "messages": msgs,
                    "logs": logs,
                }
            )
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/pc-mode":
            try:
                body = self._read_body_json()
                mode = str(body.get("mode") or "safe").strip()
                if mode not in ("off", "safe", "full"):
                    self._send_json({"ok": False, "error": "无效的权限挡位"}, HTTPStatus.BAD_REQUEST)
                    return
                _write_pc_mode(mode)
                self._send_json({"ok": True, "mode": mode})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/audio-output":
            try:
                body = self._read_body_json()
                target = str(body.get("target") or "").strip().lower()
                volume = body.get("volume", None)
                if not target:
                    target = pc_audio_router.read_audio_output_config()["target"]
                if target not in ("esp32", "pc"):
                    self._send_json({"ok": False, "error": "无效的音频输出目标"}, HTTPStatus.BAD_REQUEST)
                    return
                saved = pc_audio_router.write_audio_output_config(target, volume)
                self._send_json(
                    {
                        "ok": True,
                        "target": saved["target"],
                        "volume": saved["volume"],
                        "pc_audio_online": pc_audio_router.is_pc_audio_bridge_online(force=True),
                    }
                )
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/reply-length":
            try:
                body = self._read_body_json()
                saved = reply_length_router.write_reply_length_config(
                    chat_chars=body.get("chat_chars", None),
                    story_chars=body.get("story_chars", None),
                    preset=body.get("preset", None),
                )
                self._send_json({"ok": True, "reply_length": saved})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/audio-test":
            try:
                body = self._read_body_json()
                vol = body.get("volume", None)
                ref_wav = os.path.join(DATA_DIR, "voices", "raiden_mei", "mei_gentle_ref.wav")
                if not os.path.isfile(ref_wav):
                    ref_wav = os.path.join(DATA_DIR, "wakeup_words_short.wav")
                with open(ref_wav, "rb") as f:
                    wav_bytes = f.read()
                ok, detail = pc_audio_router.play_wav_on_pc_sync(wav_bytes, volume=vol)
                if ok:
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": f"电脑音响桥接不可用: {detail}"}, HTTPStatus.BAD_GATEWAY)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/config":
            try:
                body = self._read_body_json()
                prompt = str(body.get("prompt") or "").strip()
                llm_provider = str(body.get("llm_provider") or "DeepSeekLLM").strip()
                model_name = str(body.get("model_name") or "").strip()
                voice = str(body.get("voice") or "__gsv_raiden_mei__").strip()
                restart_now = bool(body.get("restart", True))

                if not prompt:
                    self._send_json({"ok": False, "error": "角色提示词不能为空"}, HTTPStatus.BAD_REQUEST)
                    return

                _write_updated_yaml(
                    prompt=prompt,
                    llm_provider=llm_provider,
                    model_name=model_name,
                    voice=voice,
                )
                self._send_json({"ok": True, "restarting": restart_now})

                if restart_now:
                    def _delayed_restart():
                        time.sleep(0.5)
                        os._exit(0)

                    threading.Thread(target=_delayed_restart, daemon=True).start()
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/api/clear-history":
            with _state_lock:
                _chat_messages.clear()
                _save_history()
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/tts-preview":
            try:
                body = self._read_body_json()
                voice = str(body.get("voice") or "__gsv_raiden_mei__").strip()
                raw_text = str(body.get("text") or "嗯，哥哥欢迎回来，今天的工作辛苦了，我已经为你泡好了热茶。").strip()
                req_emotion = str(body.get("emotion") or "auto").strip().lower()
                tag_emo, stripped_text = mei_emotion_router.extract_and_strip_emotion(
                    raw_text, flush_incomplete=True
                )
                clean_text = mei_emotion_router.clean_segment_for_tts(stripped_text) or "欢迎回来，今天辛苦了。"
                if voice in GSV_PROFILES:
                    configured_gsv = _get_configured_gsv_voice()
                    with _gsv_weights_lock:
                        _ensure_gsv_weights(voice, persist_boot_yaml=False)
                        try:
                            req_payload = dict(GSV_PROFILES[voice]["block"])
                            req_payload["text"] = clean_text[:120]
                            req_payload["text_split_method"] = "cut0"
                            if voice == "__gsv_raiden_mei__":
                                explicit_emo = None
                                if req_emotion and req_emotion != "auto":
                                    norm_emo, _ = mei_emotion_router.normalize_emotion_name(req_emotion)
                                    explicit_emo = norm_emo or req_emotion
                                elif tag_emo:
                                    explicit_emo = tag_emo
                                emo_key = mei_emotion_router.resolve_mei_emotion(
                                    clean_text,
                                    explicit_segment_emotion=explicit_emo,
                                )
                                emo_prof = mei_emotion_router.get_mei_emotion_profile(emo_key)
                                req_payload["ref_audio_path"] = emo_prof["ref_audio_path"]
                                req_payload["aux_ref_audio_paths"] = []
                                req_payload["prompt_text"] = emo_prof["prompt_text"]
                                req_payload["prompt_lang"] = emo_prof.get("prompt_lang", "zh")
                                req_payload["speed_factor"] = emo_prof["speed_factor"]
                                req_payload["temperature"] = emo_prof["temperature"]
                                req_payload["top_k"] = emo_prof["top_k"]
                                req_payload["top_p"] = emo_prof["top_p"]
                            for drop_k in ("type", "url", "output_dir"):
                                req_payload.pop(drop_k, None)
                            r = httpx.post(f"{GSV_Host}/tts", json=req_payload, timeout=25.0)
                            if r.status_code != 200:
                                raise RuntimeError(f"本地 GPT-SoVITS 未就绪或返回异常 ({r.status_code})")
                            wav_bytes = r.content
                        finally:
                            # Restore live configured ESP32 GSV weights if user only previewed another GSV voice
                            if configured_gsv and configured_gsv != voice:
                                try:
                                    _ensure_gsv_weights(configured_gsv, persist_boot_yaml=False)
                                except Exception:
                                    pass
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                else:
                    mp3_bytes = asyncio.run(_synthesize_mp3(text=clean_text[:120], voice=voice))
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(mp3_bytes)))
                    self.end_headers()
                    self.wfile.write(mp3_bytes)
            except Exception as e:
                self._send_json({"ok": False, "error": f"音色试听合成失败: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def start_console_background():
    watcher = threading.Thread(target=_log_watcher_loop, name="console-log-watcher", daemon=True)
    watcher.start()

    def _boot_sync():
        time.sleep(2.0)
        cfg = _read_raw_yaml()
        if cfg.get("prompt"):
            _sync_persona_to_anima(str(cfg["prompt"]))

    threading.Thread(target=_boot_sync, name="anima-persona-sync", daemon=True).start()

    def _serve():
        httpd = ThreadingHTTPServer(("0.0.0.0", CONSOLE_PORT), ConsoleHandler)
        print(f"[WebConsole] 本地可视化控制台已启动: http://localhost:{CONSOLE_PORT}", flush=True)
        httpd.serve_forever()

    srv_thread = threading.Thread(target=_serve, name="console-http-server", daemon=True)
    srv_thread.start()
