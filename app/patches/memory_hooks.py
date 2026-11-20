"""Anima Brain memory factory, Dialogue hooks, and ConnectionHandler turn-flush patches (H-1, B-5, C-8).

Zero import-time side effects: call `install_anima_memory_factory_patch()`,
`install_anima_dialogue_hooks_patch()`, and `install_chat_turn_flush_patch()` explicitly from `boot.install_all()`.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from typing import Any

import patch_registry
import runtime_events
import turn_context_router

_patched_create_memory = None
_orig_put = None
_orig_get_llm_dialogue_with_memory = None
_pre_flush_hook_registered = False


def _wrap_create_memory(_orig):
    @functools.wraps(_orig)
    def _patched_create_memory(class_name, *args, **kwargs):
        if class_name == "anima_brain":
            import anima_memory_provider

            return anima_memory_provider.MemoryProvider(*args, **kwargs)
        return _orig(class_name, *args, **kwargs)

    return _patched_create_memory


def install_anima_memory_factory_patch() -> Any:
    """Register custom Anima Brain memory provider in core.utils.memory.create_instance."""
    global _patched_create_memory
    import core.utils.memory as memory_factory

    patch_registry.install_patch(
        "anima_memory_factory",
        memory_factory,
        "create_instance",
        _wrap_create_memory,
        category="experience",
        target="core.utils.memory.create_instance",
        required_params=("class_name",),
        require_async=False,
        rebind_modules=True,
        flag_obj=memory_factory,
        flag_attr="_anima_factory_patched",
    )
    _patched_create_memory = memory_factory.create_instance
    return _patched_create_memory


# key: id(dialogue) -> {"gen": int, "kind": "loop"|"cond", "handle": TimerHandle|None,
#                        "loop": AbstractEventLoop|None, "deadline": float,
#                        "dialogue": Dialogue, "pair": dict, "user_msg": Any, "req_id": str}
_debounce_timers: dict[int, dict] = {}
_timers_lock = threading.Lock()
_timers_cond = threading.Condition(_timers_lock)
_sync_flush_thread: threading.Thread | None = None
_debounce_gen: int = 0
_FALLBACK_DEBOUNCE_SECONDS = 1.5


def _cancel_loop_handle_safe(loop, handle) -> None:
    if handle is None:
        return
    try:
        if loop is not None and not loop.is_closed():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                handle.cancel()
            else:
                loop.call_soon_threadsafe(handle.cancel)
        else:
            handle.cancel()
    except Exception:
        try:
            handle.cancel()
        except Exception:
            pass


def _cancel_dialogue_timer(dialogue) -> None:
    did = id(dialogue)
    with _timers_lock:
        entry = _debounce_timers.pop(did, None)
        if entry is not None:
            _timers_cond.notify_all()
    if entry is not None and entry.get("kind") == "loop":
        _cancel_loop_handle_safe(entry.get("loop"), entry.get("handle"))


def _commit_pair_snapshot(dialogue, pair_snapshot: dict, user_msg=None, *, mark_closed: bool = True) -> bool:
    if mark_closed and user_msg is not None:
        try:
            user_msg._turn_closed = True
        except Exception:
            pass
    return _push_pair_if_changed(dialogue, dict(pair_snapshot))


def _ensure_sync_flush_worker_locked() -> None:
    """Lazy single daemon worker for non-asyncio fallback flushes (zero per-turn threading.Timer)."""
    global _sync_flush_thread
    if _sync_flush_thread is not None and _sync_flush_thread.is_alive():
        return

    def _worker() -> None:
        while True:
            due_entries: list[dict] = []
            with _timers_cond:
                while True:
                    now = time.monotonic()
                    cond_items = [
                        (did, ent)
                        for did, ent in _debounce_timers.items()
                        if ent.get("kind") == "cond"
                    ]
                    if not cond_items:
                        _timers_cond.wait(timeout=30.0)
                        continue
                    next_deadline = min(ent["deadline"] for _, ent in cond_items)
                    if next_deadline <= now:
                        for did, ent in cond_items:
                            if ent["deadline"] <= now:
                                _debounce_timers.pop(did, None)
                                due_entries.append(ent)
                        break
                    _timers_cond.wait(timeout=max(0.005, next_deadline - now))
            for ent in due_entries:
                try:
                    _commit_pair_snapshot(
                        ent["dialogue"],
                        ent["pair"],
                        user_msg=ent.get("user_msg"),
                        mark_closed=True,
                    )
                except Exception:
                    pass

    t = threading.Thread(target=_worker, name="dialogue-debounce-flush", daemon=True)
    t.start()
    _sync_flush_thread = t


def _resolve_dialogue_event_loop(dialogue):
    try:
        loop = asyncio.get_running_loop()
        if loop is not None and not loop.is_closed():
            return loop
    except RuntimeError:
        pass
    attached = getattr(dialogue, "_loop", None)
    if attached is not None and not attached.is_closed() and attached.is_running():
        return attached
    return None


def _schedule_dialogue_fallback_flush(dialogue, pair_snapshot: dict, user_msg=None) -> None:
    """Schedule a snapshot-isolated fallback flush using event-loop call_later (no per-turn threading.Timer)."""
    global _debounce_gen
    did = id(dialogue)
    req_id = str(pair_snapshot.get("request_id") or "")
    delay = max(0.005, float(_FALLBACK_DEBOUNCE_SECONDS))
    loop = _resolve_dialogue_event_loop(dialogue)

    with _timers_lock:
        old_entry = _debounce_timers.pop(did, None)
        _debounce_gen += 1
        gen = _debounce_gen

        if loop is not None:
            entry = {
                "gen": gen,
                "kind": "loop",
                "handle": None,
                "loop": loop,
                "deadline": time.monotonic() + delay,
                "dialogue": dialogue,
                "pair": dict(pair_snapshot),
                "user_msg": user_msg,
                "req_id": req_id,
            }
            _debounce_timers[did] = entry

            def _fire_from_loop(key=did, expected_gen=gen):
                with _timers_lock:
                    cur = _debounce_timers.get(key)
                    if cur is None or cur.get("gen") != expected_gen:
                        return
                    _debounce_timers.pop(key, None)
                try:
                    _commit_pair_snapshot(
                        cur["dialogue"],
                        cur["pair"],
                        user_msg=cur.get("user_msg"),
                        mark_closed=True,
                    )
                except Exception:
                    pass

            def _arm_on_loop(ent=entry, d=delay, cb=_fire_from_loop):
                with _timers_lock:
                    if _debounce_timers.get(did) is not ent:
                        return
                    try:
                        ent["handle"] = loop.call_later(d, cb)
                    except Exception:
                        pass

            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            try:
                if running is loop:
                    entry["handle"] = loop.call_later(delay, _fire_from_loop)
                else:
                    loop.call_soon_threadsafe(_arm_on_loop)
            except Exception:
                # Event loop closed concurrently: fall back to single condition scheduler
                entry["kind"] = "cond"
                entry["loop"] = None
                entry["handle"] = None
                _ensure_sync_flush_worker_locked()
                _timers_cond.notify_all()
        else:
            _debounce_timers[did] = {
                "gen": gen,
                "kind": "cond",
                "handle": None,
                "loop": None,
                "deadline": time.monotonic() + delay,
                "dialogue": dialogue,
                "pair": dict(pair_snapshot),
                "user_msg": user_msg,
                "req_id": req_id,
            }
            _ensure_sync_flush_worker_locked()
            _timers_cond.notify_all()

    if old_entry is not None and old_entry.get("kind") == "loop":
        _cancel_loop_handle_safe(old_entry.get("loop"), old_entry.get("handle"))


def _push_pair_if_changed(dialogue, pair: dict) -> bool:
    if not pair:
        return False
    req_id = str(pair.get("request_id") or "")
    reply = str(pair.get("assistant_reply") or "")
    used_tools = bool(pair.get("used_tools", False))
    pushed_map = getattr(dialogue, "_anima_pushed_state", None)
    if not isinstance(pushed_map, dict):
        pushed_map = {}
        try:
            dialogue._anima_pushed_state = pushed_map
        except Exception:
            pass
    prev = pushed_map.get(req_id)
    if prev is not None:
        prev_reply, prev_tools = prev
        # Skip exact duplicate or shorter downgrade; allow pushing when reply grew or tool flag upgraded
        if len(reply) < len(prev_reply):
            return False
        if reply == prev_reply and (not used_tools or prev_tools):
            return False
    pushed_map[req_id] = (reply, used_tools)
    if len(pushed_map) > 128:
        oldest_key = next(iter(pushed_map))
        pushed_map.pop(oldest_key, None)
    import anima_memory_provider

    anima_memory_provider.push_turns_async([pair])
    return True


def _find_latest_real_user(messages):
    for m in reversed(list(messages or ())):
        if getattr(m, "is_temporary", False):
            continue
        if getattr(m, "role", "") == "user":
            txt = (getattr(m, "content", None) or "").strip()
            if txt and not txt.startswith("[系统提示]"):
                return m
    return None


def _mark_latest_user_attr(dialogue, attr: str, value) -> None:
    u = _find_latest_real_user(getattr(dialogue, "dialogue", None))
    if u is not None:
        try:
            setattr(u, attr, value)
        except Exception:
            pass


def _flush_dialogue_latest_pair(dialogue, *, messages_slice=None, mark_closed: bool = False) -> bool:
    import anima_memory_provider

    # C-8: Snapshot list copy so concurrent Dialogue.put mutations never race with iteration
    msgs = list(dialogue.dialogue if messages_slice is None else messages_slice)
    dev_id = str(getattr(dialogue, "_device_id", "") or "")
    pairs = anima_memory_provider.extract_clean_pairs(msgs, device_id=dev_id)
    if mark_closed:
        u = _find_latest_real_user(msgs)
        if u is not None:
            try:
                u._turn_closed = True
            except Exception:
                pass
    if not pairs:
        return False
    return _push_pair_if_changed(dialogue, pairs[-1])


def _flush_all_pending_dialogues() -> None:
    """Pre-exit hook: cancel all pending fallback handles and flush their snapshots into outbox."""
    with _timers_lock:
        entries = list(_debounce_timers.values())
        _debounce_timers.clear()
        _timers_cond.notify_all()
    for ent in entries:
        if ent.get("kind") == "loop":
            _cancel_loop_handle_safe(ent.get("loop"), ent.get("handle"))
        try:
            pair = ent.get("pair")
            if pair:
                _commit_pair_snapshot(
                    ent["dialogue"],
                    pair,
                    user_msg=ent.get("user_msg"),
                    mark_closed=False,
                )
            else:
                _flush_dialogue_latest_pair(ent["dialogue"])
        except Exception:
            pass



def _patched_put(self, message, *args, **kwargs):
    if not isinstance(getattr(message, "_occurred_at", None), (int, float)) or isinstance(
        getattr(message, "_occurred_at", None), bool
    ):
        try:
            message._occurred_at = time.time()
        except Exception:
            pass

    if _orig_put is not None:
        _orig_put(self, message, *args, **kwargs)
    else:
        self.dialogue.append(message)
    try:
        if getattr(message, "is_temporary", False):
            return
        role = getattr(message, "role", "")
        content = (getattr(message, "content", None) or "").strip()
        dev_id = str(getattr(self, "_device_id", "") or "")

        # 1. A new real user utterance proves the previous turn is finished: flush previous turn immediately.
        if role == "user":
            if content.startswith("[系统提示]"):
                return
            if content:
                try:
                    runtime_events.publish_event(
                        "user_message",
                        text=content,
                        device_id=dev_id,
                    )
                except Exception:
                    pass
            _cancel_dialogue_timer(self)
            if len(self.dialogue) > 1:
                _flush_dialogue_latest_pair(self, messages_slice=self.dialogue[:-1], mark_closed=True)
            return

        # 2. Tool call or tool result means the current turn is mid-flight: never flush a half-reply yet.
        raw_tc = getattr(message, "tool_calls", None)
        has_tc = isinstance(raw_tc, (list, tuple)) and len(raw_tc) > 0
        if role == "tool" or (role == "assistant" and has_tc):
            _mark_latest_user_attr(self, "_turn_closed", False)
            _cancel_dialogue_timer(self)
            return

        if role != "assistant" or not content:
            return

        # 3. Inside ConnectionHandler.chat(): wait until depth=0 exits so multi-step tool turns commit complete replies.
        if int(getattr(self, "_in_chat_depth", 0) or 0) > 0:
            _cancel_dialogue_timer(self)
            try:
                import mei_emotion_router as _mer

                clean_ai = _mer.strip_emotion_tags(content).strip()
            except Exception:
                clean_ai = content
            if clean_ai:
                try:
                    runtime_events.publish_event(
                        "assistant_message",
                        text=clean_ai,
                        device_id=dev_id,
                        standalone=False,
                    )
                except Exception:
                    pass
            return

        # 3b. Standalone assistant greetings (e.g. helloHandle wakeup response / incoming call text)
        #     arriving outside chat() after a previous turn already closed must NOT corrupt the prior turn.
        latest_user = _find_latest_real_user(self.dialogue[:-1])
        if latest_user is not None and getattr(latest_user, "_turn_closed", False) is True:
            try:
                message._standalone_assistant = True
            except Exception:
                pass
            try:
                import mei_emotion_router as _mer

                clean_ai = _mer.strip_emotion_tags(content).strip()
            except Exception:
                clean_ai = content
            if clean_ai:
                try:
                    runtime_events.publish_event(
                        "assistant_message",
                        text=clean_ai,
                        device_id=dev_id,
                        standalone=True,
                    )
                except Exception:
                    pass
            return

        # 4. Outside chat() (e.g. direct intentHandler reply): emit event & schedule fallback debounce timer.
        try:
            import mei_emotion_router as _mer

            clean_ai = _mer.strip_emotion_tags(content).strip()
        except Exception:
            clean_ai = content
        if clean_ai:
            try:
                runtime_events.publish_event(
                    "assistant_message",
                    text=clean_ai,
                    device_id=dev_id,
                    standalone=False,
                )
            except Exception:
                pass
        import anima_memory_provider

        msgs_snapshot = list(self.dialogue)
        pairs = anima_memory_provider.extract_clean_pairs(msgs_snapshot, device_id=dev_id)
        if not pairs:
            return
        latest_pair = dict(pairs[-1])
        latest_user_ref = _find_latest_real_user(msgs_snapshot)
        _schedule_dialogue_fallback_flush(self, latest_pair, user_msg=latest_user_ref)
    except Exception:
        pass


def _patched_get_llm_dialogue_with_memory(self, *args, **kwargs):
    if args:
        memory_str = args[0]
        fwd_args = (None,) + tuple(args[1:])
        fwd_kwargs = dict(kwargs)
    else:
        memory_str = kwargs.get("memory_str")
        fwd_args = ()
        fwd_kwargs = dict(kwargs)
        fwd_kwargs["memory_str"] = None
    # Pass memory_str=None to upstream so upstream's non-anchored re.sub('<memory>.*?</memory>')
    # cannot swallow <core_rules>/<tool_usage> if persona prose mentions '<memory>' inline;
    # turn_context_router handles memory_str safely in both split and legacy modes.
    msgs = _orig_get_llm_dialogue_with_memory(self, *fwd_args, **fwd_kwargs)
    try:
        return turn_context_router.apply_turn_context_to_messages(
            msgs,
            memory_str=memory_str,
        )
    except Exception:
        if memory_str and msgs and msgs[0].get("role") == "system":
            if memory_str not in msgs[0].get("content", ""):
                msgs[0]["content"] += f"\n\n<memory>\n{memory_str}\n</memory>"
        try:
            import reply_length_router

            reply_length_router.inject_reply_length_into_messages(msgs)
        except Exception:
            pass
        return msgs




def install_anima_dialogue_hooks_patch() -> None:
    """Install Dialogue.put / get_llm_dialogue_with_memory hooks and pre-flush callback."""
    global _orig_put, _orig_get_llm_dialogue_with_memory, _pre_flush_hook_registered
    import anima_memory_provider
    import core.utils.dialogue as dialogue_mod

    if not _pre_flush_hook_registered:
        anima_memory_provider.register_pre_flush_hook(_flush_all_pending_dialogues)
        _pre_flush_hook_registered = True

    if getattr(dialogue_mod.Dialogue, "_anima_dialogue_patched", False):
        if patch_registry.get_patch_status("anima_dialogue_hooks") is None:
            patch_registry.record_patch(
                "anima_dialogue_hooks",
                "ok",
                target="Dialogue.put/get_llm_dialogue_with_memory",
                detail="already applied",
                contract_ok=True,
                category="experience",
            )
        return

    _orig_put = dialogue_mod.Dialogue.put
    _orig_get_llm_dialogue_with_memory = dialogue_mod.Dialogue.get_llm_dialogue_with_memory
    _ok_put, _det_put = patch_registry.verify_signature_contract(
        _orig_put, required_params=("self", "message"), require_async=False
    )
    _ok_mem_d, _det_mem_d = patch_registry.verify_signature_contract(
        _orig_get_llm_dialogue_with_memory, required_params=("self", "memory_str"), require_async=False
    )
    if not (_ok_put and _ok_mem_d):
        patch_registry.record_patch(
            "anima_dialogue_hooks",
            "skipped_contract",
            target="Dialogue.put/get_llm_dialogue_with_memory",
            detail=f"{_det_put}; {_det_mem_d}",
            contract_ok=False,
            category="experience",
        )
        print(f"[AnimaDialogue] 跳过挂载对话钩子（签名契约不匹配）: {_det_put}; {_det_mem_d}", flush=True)
    else:
        functools.update_wrapper(_patched_put, _orig_put)
        functools.update_wrapper(_patched_get_llm_dialogue_with_memory, _orig_get_llm_dialogue_with_memory)
        dialogue_mod.Dialogue.put = _patched_put
        dialogue_mod.Dialogue.get_llm_dialogue_with_memory = _patched_get_llm_dialogue_with_memory
        dialogue_mod.Dialogue._anima_dialogue_patched = True
        patch_registry.record_patch(
            "anima_dialogue_hooks",
            "ok",
            target="Dialogue.put/get_llm_dialogue_with_memory",
            detail=f"{_det_put}; {_det_mem_d}",
            contract_ok=True,
            category="experience",
        )


def install_chat_turn_flush_patch() -> None:
    """Wrap ConnectionHandler.chat so Anima memory flushes once the full turn (including tool rounds) finishes."""
    try:
        from core.connection import ConnectionHandler

        if getattr(ConnectionHandler, "_anima_turn_flush_patched", False):
            if patch_registry.get_patch_status("anima_turn_flush") is None:
                patch_registry.record_patch("anima_turn_flush", "ok", target="ConnectionHandler.chat", detail="already applied")
            return
        _orig_chat = ConnectionHandler.chat
        ok_chat, det_chat = patch_registry.verify_signature_contract(
            _orig_chat, required_params=("self", "query"), require_async=False
        )
        ok_fn = True
        det_fn = "not_present"
        has_handle_fn = hasattr(ConnectionHandler, "_handle_function_result")
        _orig_handle_fn = ConnectionHandler._handle_function_result if has_handle_fn else None
        if has_handle_fn:
            ok_fn, det_fn = patch_registry.verify_signature_contract(
                _orig_handle_fn, required_params=("self", "tool_results"), require_async=False
            )

        if not (ok_chat and ok_fn):
            patch_registry.record_patch(
                "anima_turn_flush",
                "skipped_contract",
                target="ConnectionHandler.chat/_handle_function_result",
                detail=f"{det_chat}; {det_fn}",
                contract_ok=False,
                category="experience",
            )
            print(
                f"[AnimaTurnFlush] 跳过挂载 ConnectionHandler.chat 补丁（签名契约不匹配）: {det_chat}; {det_fn}",
                flush=True,
            )
            return

        @functools.wraps(_orig_chat)
        def _patched_chat(self, *args, **kwargs):
            dlg = getattr(self, "dialogue", None)
            dev_id = str(getattr(self, "device_id", "") or "")
            if dlg is not None:
                if dev_id:
                    try:
                        dlg._device_id = dev_id
                    except Exception:
                        pass
                conn_loop = getattr(self, "loop", None)
                if conn_loop is not None:
                    try:
                        dlg._loop = conn_loop
                    except Exception:
                        pass
                try:
                    dlg._in_chat_depth = int(getattr(dlg, "_in_chat_depth", 0) or 0) + 1
                except Exception:
                    pass
            chat_res = None
            chat_raised = False
            try:
                chat_res = _orig_chat(self, *args, **kwargs)
                return chat_res
            except Exception:
                chat_raised = True
                raise
            finally:
                if dlg is not None:
                    new_depth = 0
                    try:
                        new_depth = max(0, int(getattr(dlg, "_in_chat_depth", 1) or 1) - 1)
                        dlg._in_chat_depth = new_depth
                    except Exception:
                        new_depth = 0
                    if new_depth == 0:
                        _cancel_dialogue_timer(dlg)
                        try:
                            _flush_dialogue_latest_pair(dlg, mark_closed=True)
                        except Exception:
                            pass
                        if (
                            (chat_raised or chat_res is None)
                            and not getattr(self, "client_is_speaking", False)
                            and getattr(self, "_pc_audio_active_sid", None) is None
                        ):
                            try:
                                runtime_events.publish_event(
                                    "tts_state",
                                    state="stop",
                                    text=None,
                                    device_id=dev_id,
                                    sentence_id=str(getattr(self, "sentence_id", "") or ""),
                                    reason="chat_error",
                                )
                            except Exception:
                                pass

        ConnectionHandler.chat = _patched_chat

        if has_handle_fn and _orig_handle_fn is not None:
            @functools.wraps(_orig_handle_fn)
            def _patched_handle_function_result(self, tool_results, *args, **kwargs):
                dlg = getattr(self, "dialogue", None)
                if dlg is not None and tool_results:
                    _mark_latest_user_attr(dlg, "_used_tools", True)
                return _orig_handle_fn(self, tool_results, *args, **kwargs)

            ConnectionHandler._handle_function_result = _patched_handle_function_result

        ConnectionHandler._anima_turn_flush_patched = True
        patch_registry.record_patch(
            "anima_turn_flush",
            "ok",
            target="ConnectionHandler.chat/_handle_function_result",
            detail=f"{det_chat}; {det_fn}",
            contract_ok=True,
            category="experience",
        )
    except Exception as _chat_exc:
        patch_registry.record_patch(
            "anima_turn_flush",
            "failed",
            target="ConnectionHandler.chat",
            detail=str(_chat_exc),
            contract_ok=False,
            category="experience",
        )
        print(f"[AnimaTurnFlush] ConnectionHandler.chat 补丁跳过: {_chat_exc}", flush=True)

