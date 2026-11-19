"""Anima Brain memory provider for xiaozhi-esp32-server (Python 3.10 compatible HTTP client).

- Sub-15ms read path (/xiaozhi/recall) before streaming TTS voice reply
- Non-blocking async write path (/xiaozhi/ingest) for grounded facts, corrections & PAD emotion
"""

import os
import threading
import httpx
from core.providers.memory.base import MemoryProviderBase, logger

TAG = __name__

_GLOBAL_CONFIG = {
    "service_url": "http://anima-brain:18787",
    "character_id": "xiaozhi",
    "token_file": "/opt/xiaozhi-esp32-server/data/anima/studio_token.txt",
}


def _read_token() -> str:
    token_path = _GLOBAL_CONFIG["token_file"]
    if os.path.isfile(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def push_turns_async(turns_payload: list):
    """Fire-and-forget background HTTP POST to Anima Brain /xiaozhi/ingest."""
    if not turns_payload:
        return

    def _send():
        token = _read_token()
        if not token:
            return
        url = _GLOBAL_CONFIG["service_url"].rstrip("/") + "/xiaozhi/ingest"
        try:
            with httpx.Client(timeout=8.0, trust_env=False) as client:
                resp = client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "character_id": _GLOBAL_CONFIG["character_id"],
                        "turns": turns_payload,
                    },
                )
                if resp.status_code == 200:
                    logger.bind(tag=TAG).info(
                        f"已向 Anima Brain 异步提交 {len(turns_payload)} 轮对话记忆与情绪更新"
                    )
                else:
                    logger.bind(tag=TAG).warning(
                        f"Anima Brain ingest 返回 HTTP {resp.status_code}: {resp.text[:160]}"
                    )
        except Exception as e:
            logger.bind(tag=TAG).warning(f"连接 Anima Brain ingest 失败: {e}")

    threading.Thread(target=_send, daemon=True).start()


def extract_clean_pairs(messages) -> list:
    """Pair genuine non-temporary user utterances with assistant text replies."""
    pairs = []
    cur_uid = None
    cur_user = None
    cur_assistant_parts = []

    for m in messages or []:
        if getattr(m, "is_temporary", False):
            continue
        role = getattr(m, "role", "")
        content = (getattr(m, "content", None) or "").strip()
        if role == "user":
            if content.startswith("[系统提示]"):
                continue
            if cur_user and cur_assistant_parts:
                pairs.append(
                    {
                        "request_id": f"xz-{cur_uid}",
                        "user_message": cur_user,
                        "assistant_reply": " ".join(cur_assistant_parts).strip(),
                    }
                )
            cur_uid = str(getattr(m, "uniq_id", ""))[:64]
            cur_user = content
            cur_assistant_parts = []
        elif role == "assistant" and content and cur_user:
            cur_assistant_parts.append(content)

    if cur_user and cur_assistant_parts:
        pairs.append(
            {
                "request_id": f"xz-{cur_uid}",
                "user_message": cur_user,
                "assistant_reply": " ".join(cur_assistant_parts).strip(),
            }
        )
    return pairs


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config)
        cfg = config or {}
        _GLOBAL_CONFIG["service_url"] = str(
            cfg.get("service_url") or "http://anima-brain:18787"
        ).rstrip("/")
        _GLOBAL_CONFIG["character_id"] = str(cfg.get("character_id") or "xiaozhi").strip()
        if cfg.get("token_file"):
            _GLOBAL_CONFIG["token_file"] = str(cfg["token_file"])
        self._client = httpx.AsyncClient(timeout=2.2, trust_env=False)
        logger.bind(tag=TAG).info(
            f"Anima Brain 记忆引擎桥接已就绪 -> {_GLOBAL_CONFIG['service_url']} (角色: {_GLOBAL_CONFIG['character_id']})"
        )

    async def query_memory(self, query: str) -> str:
        token = _read_token()
        if not token:
            return ""
        url = _GLOBAL_CONFIG["service_url"] + "/xiaozhi/recall"
        try:
            resp = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "character_id": _GLOBAL_CONFIG["character_id"],
                    "query": query or "",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                ctx = (data.get("context_text") or "").strip()
                logger.bind(tag=TAG).info(
                    f"Anima Brain 极速召回成功: 事实={data.get('facts_count', 0)}, "
                    f"待跟进={data.get('open_loops_count', 0)}, 状态={data.get('mood', '')}"
                )
                return ctx
        except Exception as e:
            logger.bind(tag=TAG).warning(f"Anima Brain recall 超时或暂不可用，降级直接回复: {e}")
        return ""

    async def save_memory(self, msgs, session_id=None):
        pairs = extract_clean_pairs(msgs)
        if pairs:
            push_turns_async(pairs)
        return "ok"
