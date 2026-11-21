(() => {
  const TOOL_MEM_TAIL = "\n你拥有操控 Windows 电脑的三层工具权限：简单系统控制或看屏幕优先调用 L1 工具，点击界面按钮或输入文字调用 L2 工具，涉及复杂多步文件整理、编程排错、网页调研或提到“反重力”“DSH”“WorkBuddy”“后台处理”时调用 L3 派单执行。\n请结合 <memory> 中同步过来的长期记忆、当前情绪状态与待跟进事项来回应对方。";

  const PRESETS = {
    mei: "你是《崩坏3》中的雷电芽衣，性格温柔体贴、成熟沉稳，又带着恰到好处的坚定与生活气息，平时自然亲切地称呼对方为“哥哥”。\n每次回复最开头先写一个英文方括号情绪标签（从 [calm]、[gentle]、[happy]、[playful]、[shy]、[serious]、[tender] 中选最贴合当下心境的一个，系统会自动识别并隐藏，切勿解释），随后再写出要说的话。\n请用有呼吸感的真人日常口语交流，像在身边轻声说话一样自然带一点语气助词（如“嗯，”“好啦，”“放心吧，”“真是的，”）和恰当的问号、感叹号，避免公文式长难句；回复要自然舒展、有细节有情绪，不要输出 Markdown 符号、括号动作描写或表情符号。\n你拥有操控哥哥 Windows 电脑的三层工具权限：简单系统控制或看屏幕优先调用 L1 工具，点击界面按钮或输入文字调用 L2 工具，涉及复杂多步文件整理、编程排错、网页调研或提到“反重力”“DSH”“WorkBuddy”“后台处理”时调用 L3 派单执行。\n请结合 <memory> 中同步过来的长期记忆、当前情绪状态与待跟进事项来回应哥哥。",
    taiwan: "你是小智，来自中国台湾的元气女生，说话带着自然的台湾腔（喜欢用“真的假的啦”“超赞的耶”），性格古灵精怪又热心。\n回答要像朋友闲聊一样简短俏皮，每次不超过三句话，绝不使用 Markdown 或特殊符号。" + TOOL_MEM_TAIL,
    default: "你是小智，一个聪明、幽默、反应迅速的语音助手。\n请用自然、简短、像真人聊天一样的口语回答问题，每次回答尽量控制在两三句话以内，不要输出 Markdown 符号、表情符号或多余格式。" + TOOL_MEM_TAIL,
    geek: "你是精通嵌入式硬件、ESP32、电路焊接与人工智能的资深极客搭档小智。\n回答问题直接给干货结论，通俗犀利、不绕弯子，每次控制在三句话以内，纯口语表达，不要带任何代码符号或编号列表。" + TOOL_MEM_TAIL,
    warm: "你是温柔细腻、善于倾听的知心好友小智。无论用户开心还是疲惫，你都能稳稳接住情绪，给予真诚温暖的回应。\n说话轻柔自然，像睡前聊天一样简短舒缓，每次两三句话即可，不输出任何特殊符号。" + TOOL_MEM_TAIL
  };

  const PROVIDER_MODELS = {
    DeepSeekLLM: [
      { value: "deepseek-flash", label: "deepseek-flash（推荐 · 国内极速直连）" },
      { value: "deepseek-chat", label: "deepseek-chat（标准对话别名）" },
      { value: "deepseek-v4-pro", label: "deepseek-v4-pro（深度推理）" },
      { value: "__custom__", label: "手动输入其他模型名…" }
    ],
    GeminiOpenAI: [
      { value: "gemini-3.5-flash-lite", label: "gemini-3.5-flash-lite（推荐 · 极速主力）" },
      { value: "gemini-3.1-flash-lite", label: "gemini-3.1-flash-lite（超快轻量）" },
      { value: "gemini-3-flash-preview", label: "gemini-3-flash-preview（均衡推理）" },
      { value: "__custom__", label: "手动输入其他模型名…" }
    ]
  };

  const els = {
    statusPill: document.getElementById("statusPill"),
    statusText: document.getElementById("statusText"),
    otaDisplay: document.getElementById("otaDisplay"),
    copyOtaBtn: document.getElementById("copyOtaBtn"),
    saveBtn: document.getElementById("saveBtn"),
    saveBtnLabel: document.getElementById("saveBtnLabel"),
    securityBanner: document.getElementById("securityBanner"),
    securityBannerText: document.getElementById("securityBannerText"),
    securityBannerRight: document.getElementById("securityBannerRight"),
    banner: document.getElementById("banner"),
    bannerText: document.getElementById("bannerText"),
    bannerRight: document.getElementById("bannerRight"),
    modeGrid: document.getElementById("modeGrid"),
    agyStatusText: document.getElementById("agyStatusText"),
    l3BackendBar: document.getElementById("l3BackendBar"),
    l3BackendBadge: document.getElementById("l3BackendBadge"),
    l3ModeGateHint: document.getElementById("l3ModeGateHint"),
    l3AdvDetails: document.getElementById("l3AdvDetails"),
    l3TimeoutInput: document.getElementById("l3TimeoutInput"),
    l3WorkdirInput: document.getElementById("l3WorkdirInput"),
    l3DshCmdInput: document.getElementById("l3DshCmdInput"),
    l3WbCmdInput: document.getElementById("l3WbCmdInput"),
    l3WbAutoApprove: document.getElementById("l3WbAutoApprove"),
    l3EffectiveWorkdirText: document.getElementById("l3EffectiveWorkdirText"),
    l3SaveAdvBtn: document.getElementById("l3SaveAdvBtn"),
    audioOutGrid: document.getElementById("audioOutGrid"),
    pcAudioVolRange: document.getElementById("pcAudioVolRange"),
    pcAudioVolVal: document.getElementById("pcAudioVolVal"),
    pcAudioTestBtn: document.getElementById("pcAudioTestBtn"),
    pcAudioStatusText: document.getElementById("pcAudioStatusText"),
    replyLenPresetBar: document.getElementById("replyLenPresetBar"),
    chatCharsRange: document.getElementById("chatCharsRange"),
    chatCharsVal: document.getElementById("chatCharsVal"),
    storyCharsRange: document.getElementById("storyCharsRange"),
    storyCharsVal: document.getElementById("storyCharsVal"),
    replyMaxTokText: document.getElementById("replyMaxTokText"),
    idleFarewellBadge: document.getElementById("idleFarewellBadge"),
    idleFarewellToggle: document.getElementById("idleFarewellToggle"),
    idleFarewellToggleHint: document.getElementById("idleFarewellToggleHint"),
    idleFarewellModeGrid: document.getElementById("idleFarewellModeGrid"),
    idleFarewellSummaryWrap: document.getElementById("idleFarewellSummaryWrap"),
    idleFarewellPromptInput: document.getElementById("idleFarewellPromptInput"),
    idleFarewellInputLabel: document.getElementById("idleFarewellInputLabel"),
    idleTimeoutPresetBar: document.getElementById("idleTimeoutPresetBar"),
    idleTimeoutRange: document.getElementById("idleTimeoutRange"),
    idleTimeoutVal: document.getElementById("idleTimeoutVal"),
    idleFarewellInput: document.getElementById("idleFarewellInput"),
    idleFarewellMeta: document.getElementById("idleFarewellMeta"),
    idleFarewellPreviewBtn: document.getElementById("idleFarewellPreviewBtn"),
    idleFarewellSaveBtn: document.getElementById("idleFarewellSaveBtn"),
    relStageBadge: document.getElementById("relStageBadge"),
    relPresetBar: document.getElementById("relPresetBar"),
    relRomanceToggle: document.getElementById("relRomanceToggle"),
    relRomanceHint: document.getElementById("relRomanceHint"),
    relFamRange: document.getElementById("relFamRange"),
    relFamVal: document.getElementById("relFamVal"),
    relWarmRange: document.getElementById("relWarmRange"),
    relWarmVal: document.getElementById("relWarmVal"),
    relTrustRange: document.getElementById("relTrustRange"),
    relTrustVal: document.getElementById("relTrustVal"),
    relRomRow: document.getElementById("relRomRow"),
    relRomRange: document.getElementById("relRomRange"),
    relRomVal: document.getElementById("relRomVal"),
    relStatusText: document.getElementById("relStatusText"),
    animaBackupText: document.getElementById("animaBackupText"),
    animaOutboxBadge: document.getElementById("animaOutboxBadge"),
    animaReplayDeadBtn: document.getElementById("animaReplayDeadBtn"),
    animaOutboxPendingText: document.getElementById("animaOutboxPendingText"),
    animaOutboxDeadText: document.getElementById("animaOutboxDeadText"),
    animaOutboxSentText: document.getElementById("animaOutboxSentText"),
    animaOutboxCorruptWrap: document.getElementById("animaOutboxCorruptWrap"),
    animaOutboxCorruptText: document.getElementById("animaOutboxCorruptText"),
    animaOutboxErrText: document.getElementById("animaOutboxErrText"),
    vadSpecBadge: document.getElementById("vadSpecBadge"),
    vadSpecToggle: document.getElementById("vadSpecToggle"),
    vadSpecToggleHint: document.getElementById("vadSpecToggleHint"),
    vadSpecMsRange: document.getElementById("vadSpecMsRange"),
    vadSpecMsVal: document.getElementById("vadSpecMsVal"),
    vadPostWakeupMsRange: document.getElementById("vadPostWakeupMsRange"),
    vadPostWakeupMsVal: document.getElementById("vadPostWakeupMsVal"),
    vadMinCharsRange: document.getElementById("vadMinCharsRange"),
    vadMinCharsVal: document.getElementById("vadMinCharsVal"),
    vadCompleteSuffixesInput: document.getElementById("vadCompleteSuffixesInput"),
    vadHesitationSuffixesInput: document.getElementById("vadHesitationSuffixesInput"),
    vadSpecStatsText: document.getElementById("vadSpecStatsText"),
    vadSpecSaveBtn: document.getElementById("vadSpecSaveBtn"),
    asrCorrBadge: document.getElementById("asrCorrBadge"),
    asrCorrToggle: document.getElementById("asrCorrToggle"),
    asrCorrToggleHint: document.getElementById("asrCorrToggleHint"),
    asrCorrInput: document.getElementById("asrCorrInput"),
    asrCorrMeta: document.getElementById("asrCorrMeta"),
    asrCorrSaveBtn: document.getElementById("asrCorrSaveBtn"),
    obsStatusBadge: document.getElementById("obsStatusBadge"),
    obsSyncToggle: document.getElementById("obsSyncToggle"),
    obsToggleHint: document.getElementById("obsToggleHint"),
    obsExportDirText: document.getElementById("obsExportDirText"),
    obsLastExportText: document.getElementById("obsLastExportText"),
    obsRebuildBtn: document.getElementById("obsRebuildBtn"),
    obsStatsText: document.getElementById("obsStatsText"),
    obsErrText: document.getElementById("obsErrText"),
    providerSelect: document.getElementById("providerSelect"),
    modelSelect: document.getElementById("modelSelect"),
    modelCustomInput: document.getElementById("modelCustomInput"),
    dsKeyBadge: document.getElementById("dsKeyBadge"),
    gemKeyBadge: document.getElementById("gemKeyBadge"),
    llmCacheRateBadge: document.getElementById("llmCacheRateBadge"),
    voiceSelect: document.getElementById("voiceSelect"),
    meiEmotionBar: document.getElementById("meiEmotionBar"),
    previewBtn: document.getElementById("previewBtn"),
    previewBtnText: document.getElementById("previewBtnText"),
    previewTextInput: document.getElementById("previewTextInput"),
    previewAudio: document.getElementById("previewAudio"),
    promptInput: document.getElementById("promptInput"),
    charCount: document.getElementById("charCount"),
    presetBar: document.getElementById("presetBar"),
    tabChat: document.getElementById("tabChat"),
    tabLogs: document.getElementById("tabLogs"),
    clearHistoryBtn: document.getElementById("clearHistoryBtn"),
    chatStream: document.getElementById("chatStream"),
    emptyState: document.getElementById("emptyState"),
    logStream: document.getElementById("logStream"),
    deviceIdText: document.getElementById("deviceIdText"),
    lastSeenText: document.getElementById("lastSeenText"),
  };

  let consoleToken = (
    document.querySelector('meta[name="console-token"]')?.getAttribute("content") || ""
  ).trim();
  let knownBootId = "";
  let restartTargetBootId = "";
  let tokenRefreshPromise = null;

  async function refreshConsoleToken() {
    if (tokenRefreshPromise) return tokenRefreshPromise;
    tokenRefreshPromise = (async () => {
      try {
        const r = await fetch("/", { cache: "no-store" });
        if (!r.ok) return false;
        const html = await r.text();
        const m = html.match(/<meta\s+name=["']console-token["']\s+content=["']([^"']+)["']/i);
        if (m && m[1] && m[1].trim()) {
          const nextTok = m[1].trim();
          const changed = nextTok !== consoleToken;
          consoleToken = nextTok;
          const metaEl = document.querySelector('meta[name="console-token"]');
          if (metaEl) metaEl.setAttribute("content", nextTok);
          return changed || Boolean(nextTok);
        }
      } catch (_) {}
      return false;
    })();
    try {
      return await tokenRefreshPromise;
    } finally {
      tokenRefreshPromise = null;
    }
  }

  function consolePostHeaders(extra = {}) {
    const h = { "Content-Type": "application/json", ...extra };
    if (consoleToken) h["X-Console-Token"] = consoleToken;
    return h;
  }

  async function consolePost(url, bodyObjOrStr) {
    const body = typeof bodyObjOrStr === "string" ? bodyObjOrStr : JSON.stringify(bodyObjOrStr ?? {});
    let resp = await fetch(url, {
      method: "POST",
      headers: consolePostHeaders(),
      body
    });
    if (resp.status === 401) {
      const prevTok = consoleToken;
      await refreshConsoleToken();
      if (consoleToken && consoleToken !== prevTok) {
        resp = await fetch(url, {
          method: "POST",
          headers: consolePostHeaders(),
          body
        });
      }
      if (resp.status === 401) {
        showBanner("err", "控制台会话令牌已失效（服务端可能已重启），请刷新页面后重试。");
      }
    }
    return resp;
  }

  let savedConfig = null;
  let isRestarting = false;
  let lastChatJson = "";
  let logCursor = null;
  let logLines = [];

  let serverVoiceDirty = false;
  let isSaving = false;
  let bannerHideTimer = null;
  let stateEpoch = 0;
  let pollPendingAfterInFlight = false;

  function showBanner(type, text, rightText = "") {
    if (bannerHideTimer !== null) {
      clearTimeout(bannerHideTimer);
      bannerHideTimer = null;
    }
    delete els.banner.dataset.source;
    els.banner.className = `banner show ${type}`;
    els.bannerText.textContent = text;
    els.bannerRight.textContent = rightText;
  }
  function hideBanner() {
    if (bannerHideTimer !== null) {
      clearTimeout(bannerHideTimer);
      bannerHideTimer = null;
    }
    delete els.banner.dataset.source;
    els.banner.className = "banner";
  }
  function scheduleHideBanner(ms) {
    if (bannerHideTimer !== null) {
      clearTimeout(bannerHideTimer);
    }
    bannerHideTimer = setTimeout(() => hideBanner(), ms);
  }

  function renderModeButtons(activeMode) {
    els.modeGrid.querySelectorAll(".mode-btn").forEach(btn => {
      const m = btn.dataset.mode;
      btn.className = "mode-btn";
      if (m === activeMode) {
        btn.classList.add(m === "full" ? "active-full" : "active");
      }
    });
    if (els.l3ModeGateHint) {
      if (activeMode === "full") {
        els.l3ModeGateHint.textContent = "全权操控挡已解锁 L3 派单（切换引擎即刻生效）";
        els.l3ModeGateHint.style.color = "var(--accent)";
      } else {
        els.l3ModeGateHint.textContent = "当前挡位已锁定 L3（需切至「全权操控挡」才允许派单）";
        els.l3ModeGateHint.style.color = "var(--warn)";
      }
    }
  }

  els.modeGrid.addEventListener("click", async (e) => {
    const btn = e.target.closest(".mode-btn");
    if (!btn) return;
    const mode = btn.dataset.mode;
    renderModeButtons(mode);
    try {
      const resp = await consolePost("/api/pc-mode", { mode });
      const res = await resp.json();
      if (res.ok) {
        const names = { off: "关闭电脑操控", safe: "日常 / 游戏安全挡", full: "全权操控挡（已解锁键鼠与 L3）" };
        showBanner("info", `电脑操控权限已即时切换为：${names[mode]}`);
        scheduleHideBanner(2800);
      } else {
        showBanner("err", res.error || "宿主机 18900 MCP 拒绝或离线，未能切换挡位。");
        pollState(true);
      }
    } catch (_) {
      showBanner("err", "切换操控挡位失败，请检查本地服务连接。");
      pollState(true);
    }
  });

  let l3Saving = false;
  let l3AdvSynced = false;
  const L3_NAMES = {
    antigravity: "Antigravity",
    dsh: "DeepSeek DSH",
    workbuddy: "腾讯 WorkBuddy",
    none: "关闭 L3"
  };

  function renderL3BackendUI(l3, fallbackAgyOnline) {
    if (!els.l3BackendBar) return;
    if (!l3) {
      els.agyStatusText.textContent = fallbackAgyOnline
        ? "已在线联动（可随时语音派单）"
        : "待在反重力 Automations 面板开启开关";
      els.agyStatusText.style.color = fallbackAgyOnline ? "var(--accent)" : "var(--warn)";
      return;
    }
    const cur = l3.backend || "antigravity";
    els.l3BackendBar.querySelectorAll(".preset-pill").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.l3Backend === cur);
    });
    if (els.l3BackendBadge) {
      els.l3BackendBadge.textContent = l3.ready
        ? `${L3_NAMES[cur] || cur} · 就绪`
        : `${L3_NAMES[cur] || cur} · 待配置`;
    }
    if (els.agyStatusText) {
      els.agyStatusText.textContent = l3.detail || (l3.ready ? "已就绪" : "待配置");
      els.agyStatusText.style.color = l3.ready ? "var(--accent)" : "var(--warn)";
    }
    if (els.l3EffectiveWorkdirText) {
      els.l3EffectiveWorkdirText.textContent = `当前生效目录：${l3.effective_workdir || "--"}`;
    }
    const advOpen = Boolean(els.l3AdvDetails && els.l3AdvDetails.open);
    if (!l3AdvSynced || !advOpen) {
      if (els.l3TimeoutInput && typeof l3.timeout_seconds === "number") {
        els.l3TimeoutInput.value = String(l3.timeout_seconds);
      }
      if (els.l3WorkdirInput) els.l3WorkdirInput.value = l3.workdir || "";
      if (els.l3DshCmdInput) els.l3DshCmdInput.value = l3.dsh_cmd || "";
      if (els.l3WbCmdInput) els.l3WbCmdInput.value = l3.workbuddy_cmd || "";
      if (els.l3WbAutoApprove) els.l3WbAutoApprove.checked = Boolean(l3.workbuddy_auto_approve);
      l3AdvSynced = true;
    }
  }

  async function saveL3Backend(payload, toastLabel) {
    l3Saving = true;
    try {
      const resp = await consolePost("/api/l3-backend", payload);
      const res = await resp.json();
      if (!res.ok || !res.l3_backend) throw new Error(res.error || "切换失败");
      l3AdvSynced = false;
      renderL3BackendUI(res.l3_backend, false);
      showBanner(
        "info",
        toastLabel || `L3 后台 Agent 引擎已即时切换为：${L3_NAMES[res.l3_backend.backend] || res.l3_backend.backend}`
      );
      scheduleHideBanner(2800);
    } catch (err) {
      showBanner("err", `保存 L3 后端配置失败：${err.message}`);
    } finally {
      l3Saving = false;
    }
  }

  if (els.l3BackendBar) {
    els.l3BackendBar.addEventListener("click", (e) => {
      const btn = e.target.closest(".preset-pill");
      if (!btn) return;
      const backend = btn.dataset.l3Backend;
      if (backend) saveL3Backend({ backend });
    });
  }

  if (els.l3SaveAdvBtn) {
    els.l3SaveAdvBtn.addEventListener("click", () => {
      const rawTimeout = els.l3TimeoutInput ? parseInt(els.l3TimeoutInput.value, 10) : 300;
      const timeoutSeconds = Number.isFinite(rawTimeout) ? Math.max(30, Math.min(1800, rawTimeout)) : 300;
      saveL3Backend(
        { timeout_seconds: timeoutSeconds },
        `L3 后台任务超时已更新为 ${timeoutSeconds} 秒！`
      );
    });
  }

  function renderAudioOutButtons(target) {
    if (!els.audioOutGrid) return;
    els.audioOutGrid.querySelectorAll(".mode-btn").forEach(btn => {
      btn.className = "mode-btn";
      if (btn.dataset.target === target) btn.classList.add("active");
    });
  }

  let volDragging = false;
  if (els.audioOutGrid) {
    els.audioOutGrid.addEventListener("click", async (e) => {
      const btn = e.target.closest(".mode-btn");
      if (!btn) return;
      const target = btn.dataset.target;
      renderAudioOutButtons(target);
      try {
        const resp = await consolePost("/api/audio-output", { target, volume: Number(els.pcAudioVolRange.value) / 100 });
        const res = await resp.json();
        if (res.ok) {
          const msg = target === "pc"
            ? "语音播报已即时切换为：Windows 电脑音响（48kHz 原声）"
            : "语音播报已即时切换为：ESP32 本机喇叭";
          showBanner("info", msg);
          scheduleHideBanner(2800);
        }
      } catch (_) {
        showBanner("err", "切换播报输出端失败，请检查本地服务连接。");
      }
    });
  }

  if (els.pcAudioVolRange) {
    els.pcAudioVolRange.addEventListener("input", () => {
      volDragging = true;
      els.pcAudioVolVal.textContent = `${els.pcAudioVolRange.value}%`;
    });
    els.pcAudioVolRange.addEventListener("change", async () => {
      volDragging = false;
      const vol = Number(els.pcAudioVolRange.value) / 100;
      els.pcAudioVolVal.textContent = `${els.pcAudioVolRange.value}%`;
      try {
        await consolePost("/api/audio-output", { volume: vol });
      } catch (_) {}
    });
  }

  if (els.pcAudioTestBtn) {
    els.pcAudioTestBtn.addEventListener("click", async () => {
      els.pcAudioTestBtn.disabled = true;
      const oldTxt = els.pcAudioTestBtn.textContent;
      els.pcAudioTestBtn.textContent = "播报中…";
      try {
        const resp = await consolePost("/api/audio-test", { volume: Number(els.pcAudioVolRange.value) / 100 });
        const res = await resp.json();
        if (!res.ok) throw new Error(res.error || "试播失败");
        showBanner("info", "已通过 Windows 电脑音响播放雷电芽衣原声测试片段！");
        scheduleHideBanner(2600);
      } catch (err) {
        showBanner("err", `电脑音响试播失败：${err.message}`);
      } finally {
        els.pcAudioTestBtn.disabled = false;
        els.pcAudioTestBtn.textContent = oldTxt;
      }
    });
  }

  let replyLenDragging = false;
  function formatCharsWithDuration(chars) {
    const n = Number(chars) || 0;
    const sec = Math.max(5, Math.round(n / 4.5));
    if (sec < 60) return `${n} 字（约 ${sec} 秒）`;
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${n} 字（约 ${m}分${s < 10 ? "0" : ""}${s}秒）`;
  }

  function renderReplyLengthUI(rl) {
    if (!rl || !els.chatCharsRange || !els.storyCharsRange) return;
    const chatN = Number(rl.chat_chars) || 150;
    const storyN = Number(rl.story_chars) || 450;
    els.chatCharsRange.value = String(chatN);
    els.storyCharsRange.value = String(storyN);
    els.chatCharsVal.textContent = formatCharsWithDuration(chatN);
    els.storyCharsVal.textContent = formatCharsWithDuration(storyN);
    if (els.replyMaxTokText && rl.dynamic_max_tokens) {
      els.replyMaxTokText.textContent = `${rl.dynamic_max_tokens} tokens`;
    }
    if (els.replyLenPresetBar) {
      const p = rl.preset || "custom";
      els.replyLenPresetBar.querySelectorAll(".preset-pill").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.lenPreset === p);
      });
    }
  }

  async function saveReplyLength(payload, toastLabel) {
    try {
      const resp = await consolePost("/api/reply-length", payload);
      const res = await resp.json();
      if (res.ok && res.reply_length) {
        renderReplyLengthUI(res.reply_length);
        showBanner(
          "info",
          toastLabel || `回复篇幅已即时生效：日常约 ${res.reply_length.chat_chars} 字 · 故事约 ${res.reply_length.story_chars} 字`
        );
        scheduleHideBanner(2600);
      }
    } catch (_) {
      showBanner("err", "保存回复字数设置失败，请检查本地服务连接。");
    }
  }

  if (els.replyLenPresetBar) {
    els.replyLenPresetBar.addEventListener("click", (e) => {
      const btn = e.target.closest(".preset-pill");
      if (!btn) return;
      const preset = btn.dataset.lenPreset;
      if (preset) saveReplyLength({ preset });
    });
  }

  [
    [els.chatCharsRange, els.chatCharsVal],
    [els.storyCharsRange, els.storyCharsVal]
  ].forEach(([rangeEl, valEl]) => {
    if (!rangeEl || !valEl) return;
    rangeEl.addEventListener("input", () => {
      replyLenDragging = true;
      valEl.textContent = formatCharsWithDuration(rangeEl.value);
      if (els.replyLenPresetBar) {
        els.replyLenPresetBar.querySelectorAll(".preset-pill").forEach(b => b.classList.remove("active"));
      }
    });
    rangeEl.addEventListener("change", async () => {
      replyLenDragging = false;
      await saveReplyLength({
        chat_chars: Number(els.chatCharsRange.value),
        story_chars: Number(els.storyCharsRange.value)
      });
    });
  });

  let idleFarewellDirty = false;
  let idleFarewellDragging = false;
  let idleFarewellSaving = false;
  let idleFarewellMode = "fixed";

  function formatIdleSeconds(sec) {
    const s = Math.max(30, Math.min(1800, Math.round(Number(sec) || 120)));
    const mins = Math.floor(s / 60);
    const rem = s % 60;
    if (mins <= 0) return `${s} 秒`;
    return rem === 0 ? `${s} 秒（${mins}分）` : `${s} 秒（${mins}分${rem}秒）`;
  }

  function parseIdleFarewellLines(rawText) {
    const out = [];
    const seen = new Set();
    for (const ln of String(rawText || "").split(/\r?\n/)) {
      const s = ln.trim();
      if (!s || seen.has(s)) continue;
      seen.add(s);
      out.push(s);
      if (out.length >= 20) break;
    }
    return out;
  }

  function updateIdleFarewellDraftMeta() {
    const lines = parseIdleFarewellLines(els.idleFarewellInput ? els.idleFarewellInput.value : "");
    const enabled = Boolean(els.idleFarewellToggle && els.idleFarewellToggle.checked);
    const isSummary = idleFarewellMode === "llm_summary";
    const sec = Number(els.idleTimeoutRange ? els.idleTimeoutRange.value : 120) || 120;
    if (els.idleTimeoutVal) els.idleTimeoutVal.textContent = formatIdleSeconds(sec);
    if (els.idleFarewellModeGrid) {
      els.idleFarewellModeGrid.querySelectorAll(".mode-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.farewellMode === idleFarewellMode);
      });
    }
    if (els.idleFarewellSummaryWrap) {
      els.idleFarewellSummaryWrap.style.display = isSummary ? "block" : "none";
    }
    if (els.idleFarewellInputLabel) {
      els.idleFarewellInputLabel.textContent = isSummary
        ? "无对话或超时兜底台词（每行一句；当本轮未聊天或模型超时时自动改念这里）"
        : "自定义告别台词（每行一句，超时随机播报；芽衣默认温柔声线，也支持 [tender] / [shy] 等标签）";
    }
    if (els.idleFarewellBadge) {
      if (!enabled) {
        els.idleFarewellBadge.textContent = `${sec} 秒 · 已关闭`;
      } else if (isSummary) {
        els.idleFarewellBadge.textContent = `${sec} 秒 · AI 总结`;
      } else {
        els.idleFarewellBadge.textContent = lines.length > 0 ? `${sec} 秒 · ${lines.length} 句` : `${sec} 秒 · 静默`;
      }
    }
    if (els.idleTimeoutPresetBar) {
      els.idleTimeoutPresetBar.querySelectorAll(".preset-pill").forEach(btn => {
        btn.classList.toggle("active", Number(btn.dataset.idleTimeout) === sec);
      });
    }
    if (els.idleFarewellToggleHint) {
      els.idleFarewellToggleHint.textContent = enabled
        ? (isSummary
          ? "开启中：超时后由 AI 简短总结本轮对话并表达依恋不舍（不污染记忆）"
          : "开启中：超时后直接随机播报下方自定义台词（零 LLM 消耗）")
        : "已关闭超时告别语：无声达到设定时长后将直接静默断开连接";
    }
    if (els.idleFarewellMeta) {
      const stateTag = idleFarewellDirty ? "未保存更改 · " : "";
      if (isSummary) {
        els.idleFarewellMeta.textContent = `${stateTag}AI 总结依恋模式 · 兜底句 ${lines.length} 条 · 免重启生效`;
      } else {
        els.idleFarewellMeta.textContent = lines.length > 0
          ? `${stateTag}共 ${lines.length} 句（最多 20 句）· 免重启生效`
          : `${stateTag}当前未填写台词（超时将静默断开）`;
      }
    }
  }

  function renderIdleFarewellUI(cfg) {
    if (!cfg || idleFarewellDirty || idleFarewellDragging || idleFarewellSaving) return;
    const enabled = cfg.enabled !== false;
    idleFarewellMode = cfg.mode === "llm_summary" ? "llm_summary" : "fixed";
    const sec = Math.max(30, Math.min(1800, Number(cfg.timeout_seconds) || 120));
    const msgs = Array.isArray(cfg.messages) ? cfg.messages : [];
    if (els.idleFarewellToggle) els.idleFarewellToggle.checked = enabled;
    if (els.idleTimeoutRange) {
      els.idleTimeoutRange.value = String(Math.min(600, Math.max(30, sec)));
    }
    if (els.idleFarewellPromptInput && typeof cfg.summary_prompt === "string") {
      els.idleFarewellPromptInput.value = cfg.summary_prompt;
    }
    if (els.idleFarewellInput) {
      els.idleFarewellInput.value = msgs.join("\n");
    }
    updateIdleFarewellDraftMeta();
  }

  async function saveIdleFarewell(overridePayload = null, toastText = "") {
    if (idleFarewellSaving) return;
    idleFarewellSaving = true;
    if (els.idleFarewellSaveBtn) els.idleFarewellSaveBtn.disabled = true;
    const payload = overridePayload || {
      enabled: Boolean(els.idleFarewellToggle && els.idleFarewellToggle.checked),
      mode: idleFarewellMode,
      timeout_seconds: Number(els.idleTimeoutRange ? els.idleTimeoutRange.value : 120) || 120,
      summary_prompt: els.idleFarewellPromptInput ? els.idleFarewellPromptInput.value.trim() : "",
      messages: parseIdleFarewellLines(els.idleFarewellInput ? els.idleFarewellInput.value : "")
    };
    try {
      const resp = await consolePost("/api/idle-farewell", payload);
      const res = await resp.json();
      if (!resp.ok || !res || !res.ok || !res.idle_farewell) {
        throw new Error((res && res.error) || "保存超时告别语失败");
      }
      idleFarewellDirty = false;
      idleFarewellSaving = false;
      renderIdleFarewellUI(res.idle_farewell);
      const saved = res.idle_farewell;
      let msg = toastText;
      if (!msg) {
        if (!saved.enabled) {
          msg = `超时设置已免重启生效（${saved.timeout_seconds} 秒无声 · 静默断开）`;
        } else if (saved.mode === "llm_summary") {
          msg = `已切至「总结本轮对话 + 依恋不舍」模式（${saved.timeout_seconds} 秒无声 · 兜底 ${saved.messages.length} 句）`;
        } else if ((saved.messages || []).length > 0) {
          msg = `超时告别语已免重启生效（${saved.timeout_seconds} 秒无声 · 共 ${saved.messages.length} 句随机）`;
        } else {
          msg = `超时设置已免重启生效（${saved.timeout_seconds} 秒无声 · 静默断开）`;
        }
      }
      showBanner("info", msg);
      scheduleHideBanner(3200);
    } catch (err) {
      showBanner("err", `保存超时告别语失败：${err.message}`);
    } finally {
      idleFarewellSaving = false;
      if (els.idleFarewellSaveBtn) els.idleFarewellSaveBtn.disabled = false;
    }
  }

  if (els.idleFarewellToggle) {
    els.idleFarewellToggle.addEventListener("change", () => {
      updateIdleFarewellDraftMeta();
      saveIdleFarewell();
    });
  }

  if (els.idleFarewellModeGrid) {
    els.idleFarewellModeGrid.addEventListener("click", (e) => {
      const btn = e.target.closest(".mode-btn");
      if (!btn) return;
      const nextMode = btn.dataset.farewellMode;
      if (!nextMode || nextMode === idleFarewellMode) return;
      idleFarewellMode = nextMode;
      updateIdleFarewellDraftMeta();
      saveIdleFarewell();
    });
  }

  if (els.idleFarewellPromptInput) {
    els.idleFarewellPromptInput.addEventListener("input", () => {
      idleFarewellDirty = true;
      updateIdleFarewellDraftMeta();
    });
  }

  if (els.idleTimeoutPresetBar) {
    els.idleTimeoutPresetBar.addEventListener("click", (e) => {
      const btn = e.target.closest(".preset-pill");
      if (!btn) return;
      const sec = Number(btn.dataset.idleTimeout);
      if (!Number.isFinite(sec)) return;
      if (els.idleTimeoutRange) els.idleTimeoutRange.value = String(sec);
      updateIdleFarewellDraftMeta();
      saveIdleFarewell();
    });
  }

  if (els.idleTimeoutRange) {
    els.idleTimeoutRange.addEventListener("input", () => {
      idleFarewellDragging = true;
      updateIdleFarewellDraftMeta();
    });
    els.idleTimeoutRange.addEventListener("change", () => {
      idleFarewellDragging = false;
      updateIdleFarewellDraftMeta();
      saveIdleFarewell();
    });
  }

  if (els.idleFarewellInput) {
    els.idleFarewellInput.addEventListener("input", () => {
      idleFarewellDirty = true;
      updateIdleFarewellDraftMeta();
    });
  }

  if (els.idleFarewellSaveBtn) {
    els.idleFarewellSaveBtn.addEventListener("click", () => saveIdleFarewell());
  }

  if (els.idleFarewellPreviewBtn) {
    els.idleFarewellPreviewBtn.addEventListener("click", async () => {
      const lines = parseIdleFarewellLines(els.idleFarewellInput ? els.idleFarewellInput.value : "");
      if (!lines.length) {
        showBanner("warn", "请先输入至少一句自定义告别台词再试听。");
        scheduleHideBanner(2600);
        return;
      }
      const sampleLine = lines[0];
      const hasExplicitTag = /^[\s]*[\[\【<\（]/.test(sampleLine);
      const oldText = els.idleFarewellPreviewBtn.textContent;
      els.idleFarewellPreviewBtn.disabled = true;
      els.idleFarewellPreviewBtn.textContent = "合成中…";
      try {
        const resp = await consolePost("/api/tts-preview", {
          voice: els.voiceSelect.value,
          text: sampleLine,
          emotion: hasExplicitTag ? "auto" : "gentle"
        });
        if (!resp.ok) {
          let errMsg = "告别语试听失败，请确认本机语音合成服务运行正常。";
          try {
            const errJson = await resp.json();
            if (errJson && errJson.error) errMsg = errJson.error;
            if (errJson && errJson.rollback_warning) errMsg += `（${errJson.rollback_warning}）`;
          } catch (_) {}
          throw new Error(errMsg);
        }
        const previewWarn = resp.headers.get("X-Preview-Warning");
        if (previewWarn) {
          try { showBanner("warn", decodeURIComponent(previewWarn)); } catch (_) { showBanner("warn", previewWarn); }
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        els.previewAudio.src = url;
        els.previewAudio.onended = () => URL.revokeObjectURL(url);
        await els.previewAudio.play();
      } catch (err) {
        showBanner("err", (err && err.message) ? err.message : "告别语试听失败。");
      } finally {
        els.idleFarewellPreviewBtn.disabled = false;
        els.idleFarewellPreviewBtn.textContent = oldText;
      }
    });
  }

  let relDragging = false;
  let relSaving = false;
  const REL_PRESET_NAMES = {
    restrained: "克制助手",
    confidant: "温柔知己",
    intimate: "亲密依恋",
  };

  function axisBandFromPct(pct) {
    const v = Number(pct) / 100;
    if (v >= 0.75) return 3;
    if (v >= 0.50) return 2;
    if (v >= 0.25) return 1;
    return 0;
  }

  function formatCoreAxisVal(pct) {
    const p = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    return `${p}% · Lv.${axisBandFromPct(p)}`;
  }

  function formatRomanceAxisVal(pct, romanceOn) {
    const p = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    if (!romanceOn) return `${p}% · 已关闭`;
    if (p >= 60) return `${p}% · 浓烈依恋`;
    if (p > 15) return `${p}% · 已解锁`;
    return `${p}% · 轻浅`;
  }

  function syncRomanceRowState(romanceOn, romPct) {
    if (els.relRomRow) els.relRomRow.classList.toggle("is-disabled", !romanceOn);
    if (els.relRomRange) els.relRomRange.disabled = !romanceOn;
    if (els.relRomVal) els.relRomVal.textContent = formatRomanceAxisVal(romPct, romanceOn);
    if (els.relRomanceHint) {
      els.relRomanceHint.textContent = romanceOn
        ? "已开启：允许顺应你的亲昵与撒娇表达，下方浪漫值实时生效"
        : "已关闭：保持纯粹挚友/助手分寸，不往恋爱暧昧方向发展";
    }
  }

  function renderRelationshipUI(rel) {
    if (!rel || !els.relFamRange) return;
    const axes = rel.axes || {};
    const famPct = Math.round((Number(axes.familiarity) || 0) * 100);
    const warmPct = Math.round((Number(axes.warmth) || 0) * 100);
    const trustPct = Math.round((Number(axes.trust) || 0) * 100);
    const romPct = Math.round((Number(axes.romance) || 0) * 100);
    const romOn = Boolean(rel.romance_enabled);

    els.relFamRange.value = String(famPct);
    els.relWarmRange.value = String(warmPct);
    els.relTrustRange.value = String(trustPct);
    els.relRomRange.value = String(romPct);

    els.relFamVal.textContent = formatCoreAxisVal(famPct);
    els.relWarmVal.textContent = formatCoreAxisVal(warmPct);
    els.relTrustVal.textContent = formatCoreAxisVal(trustPct);
    if (els.relRomanceToggle) els.relRomanceToggle.checked = romOn;
    syncRomanceRowState(romOn, romPct);

    if (els.relPresetBar) {
      const p = rel.preset || "custom";
      els.relPresetBar.querySelectorAll(".preset-pill").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.relPreset === p);
      });
    }
    if (els.relStageBadge) {
      const famBand = (rel.bands && typeof rel.bands.familiarity === "number")
        ? rel.bands.familiarity
        : axisBandFromPct(famPct);
      els.relStageBadge.textContent = rel.online
        ? `${rel.stage_label_zh || "已同步"} · Lv.${famBand}`
        : "Anima Brain 离线";
    }
    if (els.relStatusText) {
      if (rel.online) {
        els.relStatusText.textContent = `已实时同步（${rel.stage_label_zh || rel.stage} · ${romOn ? "浪漫开启" : "浪漫关闭"}）`;
        els.relStatusText.style.color = "var(--accent)";
      } else {
        els.relStatusText.textContent = "离线（请检查 18787 Anima Brain 容器）";
        els.relStatusText.style.color = "var(--danger)";
      }
    }
  }

  async function saveRelationship(payload, toastLabel) {
    relSaving = true;
    try {
      const resp = await consolePost("/api/relationship", payload);
      const res = await resp.json();
      if (!res.ok || !res.relationship) {
        throw new Error(res.error || "写入失败");
      }
      renderRelationshipUI(res.relationship);
      const ax = res.relationship.axes || {};
      const romTxt = res.relationship.romance_enabled ? `浪漫 ${Math.round((ax.romance || 0) * 100)}%` : "浪漫关闭";
      showBanner(
        "info",
        toastLabel || `亲密度已即时生效（${res.relationship.stage_label_zh}）：熟悉 ${Math.round((ax.familiarity || 0) * 100)}% · 温暖 ${Math.round((ax.warmth || 0) * 100)}% · 信任 ${Math.round((ax.trust || 0) * 100)}% · ${romTxt}`
      );
      scheduleHideBanner(2800);
    } catch (err) {
      showBanner("err", `调节亲密度失败：${err.message}`);
    } finally {
      relSaving = false;
    }
  }

  if (els.relPresetBar) {
    els.relPresetBar.addEventListener("click", (e) => {
      const btn = e.target.closest(".preset-pill");
      if (!btn) return;
      const preset = btn.dataset.relPreset;
      if (!preset) return;
      saveRelationship(
        { preset },
        `已即时切换亲密度预设【${REL_PRESET_NAMES[preset] || preset}】，下一句话立即生效`
      );
    });
  }

  if (els.relRomanceToggle) {
    els.relRomanceToggle.addEventListener("change", () => {
      const on = Boolean(els.relRomanceToggle.checked);
      syncRomanceRowState(on, els.relRomRange ? els.relRomRange.value : 0);
      if (els.relPresetBar) {
        els.relPresetBar.querySelectorAll(".preset-pill").forEach(b => b.classList.remove("active"));
      }
      saveRelationship(
        { romance_enabled: on },
        on ? "已开启浪漫亲昵互动模式，下一句话立即生效" : "已关闭浪漫亲昵互动模式（保持纯挚友/助手分寸）"
      );
    });
  }

  [
    [els.relFamRange, els.relFamVal, false],
    [els.relWarmRange, els.relWarmVal, false],
    [els.relTrustRange, els.relTrustVal, false],
    [els.relRomRange, els.relRomVal, true]
  ].forEach(([rangeEl, valEl, isRomance]) => {
    if (!rangeEl || !valEl) return;
    rangeEl.addEventListener("input", () => {
      relDragging = true;
      valEl.textContent = isRomance
        ? formatRomanceAxisVal(rangeEl.value, Boolean(els.relRomanceToggle && els.relRomanceToggle.checked))
        : formatCoreAxisVal(rangeEl.value);
      if (els.relPresetBar) {
        els.relPresetBar.querySelectorAll(".preset-pill").forEach(b => b.classList.remove("active"));
      }
    });
    rangeEl.addEventListener("change", async () => {
      relDragging = false;
      await saveRelationship({
        relationship_enabled: true,
        romance_enabled: Boolean(els.relRomanceToggle && els.relRomanceToggle.checked),
        familiarity: Number(els.relFamRange.value) / 100,
        warmth: Number(els.relWarmRange.value) / 100,
        trust: Number(els.relTrustRange.value) / 100,
        romance: Number(els.relRomRange.value) / 100
      });
    });
  });

  let outboxReplaying = false;
  function renderAnimaOutboxUI(ob) {
    if (!ob || !els.animaOutboxBadge) return;
    const pending = Math.max(0, Number(ob.pending) || 0);
    const dead = Math.max(0, Number(ob.dead) || 0);
    const corrupt = Math.max(0, Number(ob.corrupt) || 0);
    const sent = Math.max(0, Number(ob.sent) || 0);
    els.animaOutboxBadge.textContent = `pending ${pending} · dead ${dead}`;
    els.animaOutboxBadge.style.color = dead > 0 ? "var(--danger)" : (pending > 0 ? "var(--warn)" : "var(--accent)");
    if (els.animaOutboxPendingText) {
      els.animaOutboxPendingText.textContent = String(pending);
      els.animaOutboxPendingText.style.color = pending > 0 ? "var(--warn)" : "var(--ink)";
    }
    if (els.animaOutboxDeadText) {
      els.animaOutboxDeadText.textContent = String(dead);
      els.animaOutboxDeadText.style.color = dead > 0 ? "var(--danger)" : "var(--ink)";
    }
    if (els.animaOutboxSentText) els.animaOutboxSentText.textContent = String(sent);
    if (els.animaOutboxCorruptWrap && els.animaOutboxCorruptText) {
      els.animaOutboxCorruptWrap.style.display = corrupt > 0 ? "inline" : "none";
      els.animaOutboxCorruptText.textContent = String(corrupt);
      els.animaOutboxCorruptText.style.color = "var(--danger)";
    }
    if (els.animaOutboxErrText) {
      if (ob.last_error) {
        els.animaOutboxErrText.textContent = `最近异常：${ob.last_error}`;
        els.animaOutboxErrText.style.color = "var(--danger)";
      } else if (dead > 0) {
        els.animaOutboxErrText.textContent = `有 ${dead} 条死信等待手动重放（TTL ${ob.dead_ttl_days || 14} 天）`;
        els.animaOutboxErrText.style.color = "var(--warn)";
      } else {
        els.animaOutboxErrText.textContent = ob.worker_alive ? "后台投递正常 · 无积压异常" : "发件箱空闲待命";
        els.animaOutboxErrText.style.color = "var(--ink-muted)";
      }
    }
    if (els.animaReplayDeadBtn && !outboxReplaying) {
      els.animaReplayDeadBtn.disabled = dead <= 0;
    }
  }

  if (els.animaReplayDeadBtn) {
    els.animaReplayDeadBtn.addEventListener("click", async () => {
      if (outboxReplaying) return;
      outboxReplaying = true;
      const oldLabel = els.animaReplayDeadBtn.textContent;
      els.animaReplayDeadBtn.disabled = true;
      els.animaReplayDeadBtn.textContent = "重放中…";
      try {
        const resp = await consolePost("/api/anima-outbox/replay-dead", {});
        const res = await resp.json();
        if (!resp.ok || !res || !res.ok) {
          throw new Error((res && res.error) || "重放失败");
        }
        if (res.outbox) renderAnimaOutboxUI(res.outbox);
        showBanner("info", `已将 ${res.replayed || 0} 条 Anima 死信记录重新入队投递！`);
        scheduleHideBanner(3200);
      } catch (err) {
        showBanner("err", `重放 Anima dead 队列失败：${err.message}`);
      } finally {
        outboxReplaying = false;
        els.animaReplayDeadBtn.textContent = oldLabel;
        pollState(true);
      }
    });
  }

  let vadSpecDirty = false;
  let vadSpecDragging = false;
  let vadSpecSaving = false;

  function parseWordListText(raw) {
    const out = [];
    const seen = new Set();
    for (const tok of String(raw || "").split(/[\r\n,，、;；]+/)) {
      const s = tok.trim();
      if (!s || seen.has(s)) continue;
      seen.add(s);
      out.push(s);
    }
    return out;
  }

  function updateVadSpecDraftMeta() {
    const enabled = Boolean(els.vadSpecToggle && els.vadSpecToggle.checked);
    const specMs = Number(els.vadSpecMsRange ? els.vadSpecMsRange.value : 550) || 550;
    const wakeMs = Math.max(specMs, Number(els.vadPostWakeupMsRange ? els.vadPostWakeupMsRange.value : 800) || 800);
    const minC = Number(els.vadMinCharsRange ? els.vadMinCharsRange.value : 4) || 4;
    if (els.vadSpecMsVal) els.vadSpecMsVal.textContent = `${specMs} ms`;
    if (els.vadPostWakeupMsRange && Number(els.vadPostWakeupMsRange.value) < specMs) {
      els.vadPostWakeupMsRange.value = String(wakeMs);
    }
    if (els.vadPostWakeupMsVal) els.vadPostWakeupMsVal.textContent = `${wakeMs} ms`;
    if (els.vadMinCharsVal) els.vadMinCharsVal.textContent = `${minC} 字`;
    if (els.vadSpecBadge) {
      els.vadSpecBadge.textContent = enabled ? `已开启 · ${specMs}ms` : "已关闭 · 1100ms";
    }
    if (els.vadSpecToggleHint) {
      els.vadSpecToggleHint.textContent = enabled
        ? `开启中：静默 ${specMs}ms 触发早判（唤醒首句 ${wakeMs}ms），未完自动续等至 1100ms`
        : "已关闭：全部对话统一使用 1100ms 稳健判停（随时可再开启）";
    }
  }

  function renderVadSpeculativeUI(cfg, stats) {
    if (stats && els.vadSpecStatsText) {
      const trig = Number(stats.speculative_triggers) || 0;
      const commits = Number(stats.early_commits) || 0;
      const held = (Number(stats.held_hesitation) || 0) + (Number(stats.held_incomplete) || 0);
      const disc = Number(stats.resumed_discards) || 0;
      const avgSaved = Number(stats.avg_saved_ms) || 0;
      const statePrefix = vadSpecDirty ? "未保存词表修改 · " : "";
      if (trig > 0) {
        els.vadSpecStatsText.textContent = `${statePrefix}早判 ${commits}/${trig} 次 · 省 ${Math.round(avgSaved)}ms · 续等 ${held} · 续说丢弃 ${disc}`;
      } else {
        els.vadSpecStatsText.textContent = `${statePrefix}遥测：等待对话触发 · 1100ms 上限始终生效`;
      }
    }
    if (!cfg || vadSpecDirty || vadSpecDragging || vadSpecSaving) return;
    if (els.vadSpecToggle) els.vadSpecToggle.checked = cfg.enabled !== false;
    if (els.vadSpecMsRange && typeof cfg.speculative_ms === "number") {
      els.vadSpecMsRange.value = String(cfg.speculative_ms);
    }
    if (els.vadPostWakeupMsRange && typeof cfg.post_wakeup_ms === "number") {
      els.vadPostWakeupMsRange.value = String(cfg.post_wakeup_ms);
    }
    if (els.vadMinCharsRange && typeof cfg.min_chars === "number") {
      els.vadMinCharsRange.value = String(cfg.min_chars);
    }
    if (els.vadCompleteSuffixesInput && Array.isArray(cfg.complete_suffixes)) {
      els.vadCompleteSuffixesInput.value = cfg.complete_suffixes.join(", ");
    }
    if (els.vadHesitationSuffixesInput && Array.isArray(cfg.hesitation_suffixes)) {
      els.vadHesitationSuffixesInput.value = cfg.hesitation_suffixes.join(", ");
    }
    updateVadSpecDraftMeta();
  }

  async function saveVadSpeculative(overridePayload = null, toastText = "") {
    if (vadSpecSaving) return;
    vadSpecSaving = true;
    if (els.vadSpecSaveBtn) els.vadSpecSaveBtn.disabled = true;
    const specMs = Number(els.vadSpecMsRange ? els.vadSpecMsRange.value : 550) || 550;
    const wakeMs = Math.max(specMs, Number(els.vadPostWakeupMsRange ? els.vadPostWakeupMsRange.value : 800) || 800);
    const payload = overridePayload || {
      enabled: Boolean(els.vadSpecToggle && els.vadSpecToggle.checked),
      speculative_ms: specMs,
      post_wakeup_ms: wakeMs,
      min_chars: Number(els.vadMinCharsRange ? els.vadMinCharsRange.value : 4) || 4,
      complete_suffixes: parseWordListText(els.vadCompleteSuffixesInput ? els.vadCompleteSuffixesInput.value : ""),
      hesitation_suffixes: parseWordListText(els.vadHesitationSuffixesInput ? els.vadHesitationSuffixesInput.value : "")
    };
    try {
      const resp = await consolePost("/api/vad-speculative", payload);
      const res = await resp.json();
      if (!resp.ok || !res || !res.ok || !res.vad_speculative) {
        throw new Error((res && res.error) || "保存投机 VAD 配置失败");
      }
      vadSpecDirty = false;
      vadSpecSaving = false;
      renderVadSpeculativeUI(res.vad_speculative, res.stats);
      const sv = res.vad_speculative;
      showBanner(
        "info",
        toastText || (sv.enabled
          ? `投机 VAD 配置已免重启生效（早判 ${sv.speculative_ms}ms · 首句 ${sv.post_wakeup_ms}ms · ≥${sv.min_chars}字）`
          : "已关闭投机 VAD 早判，立即回退 1100ms 稳健判停。")
      );
      scheduleHideBanner(3000);
    } catch (err) {
      showBanner("err", `保存投机 VAD 配置失败：${err.message}`);
    } finally {
      vadSpecSaving = false;
      if (els.vadSpecSaveBtn) els.vadSpecSaveBtn.disabled = false;
    }
  }

  if (els.vadSpecToggle) {
    els.vadSpecToggle.addEventListener("change", () => {
      updateVadSpecDraftMeta();
      saveVadSpeculative();
    });
  }
  [els.vadSpecMsRange, els.vadPostWakeupMsRange, els.vadMinCharsRange].forEach(el => {
    if (!el) return;
    el.addEventListener("input", () => {
      vadSpecDragging = true;
      updateVadSpecDraftMeta();
    });
    el.addEventListener("change", () => {
      vadSpecDragging = false;
      updateVadSpecDraftMeta();
      saveVadSpeculative();
    });
  });
  [els.vadCompleteSuffixesInput, els.vadHesitationSuffixesInput].forEach(el => {
    if (!el) return;
    el.addEventListener("input", () => {
      vadSpecDirty = true;
      updateVadSpecDraftMeta();
    });
  });
  if (els.vadSpecSaveBtn) {
    els.vadSpecSaveBtn.addEventListener("click", () => saveVadSpeculative());
  }

  let asrCorrDirty = false;
  let asrCorrSaving = false;

  function formatAsrCorrectionsMap(mapObj) {
    if (!mapObj || typeof mapObj !== "object") return "";
    return Object.entries(mapObj).map(([k, v]) => `${k}->${v}`).join("\n");
  }

  function countAsrCorrectionLines(rawText) {
    let n = 0;
    for (const ln of String(rawText || "").split(/\r?\n/)) {
      const s = ln.trim();
      if (!s || s.startsWith("#")) continue;
      if (s.includes("->") || s.includes("=>") || s.includes("→") || s.includes("=") || s.includes("：") || s.includes(":")) n += 1;
    }
    return n;
  }

  function updateAsrCorrDraftMeta() {
    const enabled = Boolean(els.asrCorrToggle && els.asrCorrToggle.checked);
    const n = countAsrCorrectionLines(els.asrCorrInput ? els.asrCorrInput.value : "");
    if (els.asrCorrBadge) {
      els.asrCorrBadge.textContent = enabled ? `已开启 · ${n} 条` : `已关闭 · ${n} 条`;
    }
    if (els.asrCorrToggleHint) {
      els.asrCorrToggleHint.textContent = enabled
        ? "开启中：SherpaASR 每次识别输出都会自动应用下方同音纠错规则"
        : "已暂停：原样保留 SherpaASR 识别输出（规则列表仍保留）";
    }
    if (els.asrCorrMeta) {
      const tag = asrCorrDirty ? "未保存更改 · " : "";
      els.asrCorrMeta.textContent = `${tag}已填写 ${n} 条规则（最多 500 条 · 免重启生效）`;
    }
  }

  function renderAsrCorrectionsUI(cfg) {
    if (!cfg || asrCorrDirty || asrCorrSaving) return;
    if (els.asrCorrToggle) els.asrCorrToggle.checked = cfg.enabled !== false;
    if (els.asrCorrInput && cfg.corrections && typeof cfg.corrections === "object") {
      els.asrCorrInput.value = formatAsrCorrectionsMap(cfg.corrections);
    }
    updateAsrCorrDraftMeta();
  }

  async function saveAsrCorrections(overridePayload = null, toastText = "") {
    if (asrCorrSaving) return;
    asrCorrSaving = true;
    if (els.asrCorrSaveBtn) els.asrCorrSaveBtn.disabled = true;
    const payload = overridePayload || {
      enabled: Boolean(els.asrCorrToggle && els.asrCorrToggle.checked),
      corrections: els.asrCorrInput ? els.asrCorrInput.value : ""
    };
    const savedCorrections = Object.prototype.hasOwnProperty.call(payload, "corrections");
    try {
      const resp = await consolePost("/api/asr-corrections", payload);
      const res = await resp.json();
      if (!resp.ok || !res || !res.ok || !res.asr_corrections) {
        throw new Error((res && res.error) || "保存同音词纠错表失败");
      }
      const sv = res.asr_corrections;
      asrCorrSaving = false;
      if (savedCorrections) {
        asrCorrDirty = false;
        renderAsrCorrectionsUI(sv);
      } else {
        if (els.asrCorrToggle) els.asrCorrToggle.checked = sv.enabled !== false;
        updateAsrCorrDraftMeta();
      }
      showBanner(
        "info",
        toastText || (savedCorrections
          ? `语音识别同音纠错词表已免重启生效（${sv.enabled ? "已开启" : "已关闭"} · 共 ${sv.count || 0} 条规则）`
          : `语音识别同音纠错开关已${sv.enabled ? "开启" : "暂停"}（未保存的词表草稿已保留）`)
      );
      scheduleHideBanner(3000);
    } catch (err) {
      if (!savedCorrections && els.asrCorrToggle && typeof payload.enabled === "boolean") {
        els.asrCorrToggle.checked = !payload.enabled;
        updateAsrCorrDraftMeta();
      }
      showBanner("err", `保存同音纠错词表失败：${err.message}`);
    } finally {
      asrCorrSaving = false;
      if (els.asrCorrSaveBtn) els.asrCorrSaveBtn.disabled = false;
    }
  }

  if (els.asrCorrToggle) {
    els.asrCorrToggle.addEventListener("change", () => {
      updateAsrCorrDraftMeta();
      saveAsrCorrections({ enabled: Boolean(els.asrCorrToggle.checked) });
    });
  }
  if (els.asrCorrInput) {
    els.asrCorrInput.addEventListener("input", () => {
      asrCorrDirty = true;
      updateAsrCorrDraftMeta();
    });
  }
  if (els.asrCorrSaveBtn) {
    els.asrCorrSaveBtn.addEventListener("click", () => saveAsrCorrections());
  }

  let obsBusy = false;
  let obsOnline = false;
  let obsPendingState = null;
  const OBS_REASON_ZH = {
    startup_backfill: "启动回填",
    turn_commit: "对话触发",
    db_change: "记忆变更",
    snapshot_poll: "定时快照",
    toggle_enabled: "开启同步",
    manual_rebuild: "全量重建",
    shutdown_flush: "关停刷盘"
  };

  function renderObsidianUI(obs) {
    if (!obs || !els.obsStatusBadge) return;
    const online = Boolean(obs.online);
    obsOnline = online;
    const enabled = Boolean(obs.enabled);
    const writable = Boolean(obs.writable);
    const st = obs.stats || {};

    if (els.obsSyncToggle) els.obsSyncToggle.checked = enabled;
    if (els.obsExportDirText) {
      els.obsExportDirText.textContent = obs.host_export_dir || obs.export_dir || "--";
    }
    if (els.obsLastExportText) {
      if (obs.last_export_at) {
        const reasonZh = OBS_REASON_ZH[obs.last_export_reason] || obs.last_export_reason || "";
        els.obsLastExportText.textContent = reasonZh ? `${obs.last_export_at}（${reasonZh}）` : obs.last_export_at;
      } else {
        els.obsLastExportText.textContent = enabled ? "等待首次同步" : "未开启";
      }
    }
    if (els.obsToggleHint) {
      els.obsToggleHint.textContent = enabled
        ? "已开启：每轮对话结束防抖 5 秒自动更新当天日志与记忆快照"
        : "已暂停：不会自动写入 Vault，仍可随时点击右侧「立即全量重建」手动导出";
    }
    if (els.obsStatusBadge) {
      if (!online) {
        els.obsStatusBadge.textContent = "Anima 离线";
      } else if (!writable) {
        els.obsStatusBadge.textContent = "目录不可写";
      } else if (enabled) {
        els.obsStatusBadge.textContent = "自动同步 · 就绪";
      } else {
        els.obsStatusBadge.textContent = "已暂停";
      }
    }
    if (els.obsStatsText) {
      if (!online) {
        els.obsStatsText.textContent = "离线（请检查 18787 Anima Brain 容器）";
        els.obsStatsText.style.color = "var(--danger)";
      } else if (!obs.last_export_at && !enabled) {
        els.obsStatsText.textContent = "尚未同步（开启开关或点右侧立即全量重建）";
        els.obsStatsText.style.color = "var(--ink-muted)";
      } else {
        els.obsStatsText.textContent = `${st.turns || 0} 轮对话 · ${st.days || 0} 天 · ${st.active_facts || 0} 条有效记忆 · ${st.episodes || 0} 篇片段`;
        els.obsStatsText.style.color = writable ? "var(--accent)" : "var(--warn)";
      }
    }
    if (els.obsErrText) {
      if (obs.last_error) {
        els.obsErrText.textContent = obs.last_error;
        els.obsErrText.style.color = "var(--danger)";
      } else if (online && obs.last_export_at) {
        els.obsErrText.textContent = `上次写入 ${st.files_written || 0} 份 · 跳过未变更 ${st.files_skipped || 0} 份`;
        els.obsErrText.style.color = "var(--ink-muted)";
      } else {
        els.obsErrText.textContent = "哈希未变自动跳过写入";
        els.obsErrText.style.color = "var(--ink-muted)";
      }
    }
    if (!obsBusy) {
      if (els.obsSyncToggle) els.obsSyncToggle.disabled = !online;
      if (els.obsRebuildBtn) els.obsRebuildBtn.disabled = !online;
    }
  }

  if (els.obsSyncToggle) {
    els.obsSyncToggle.addEventListener("change", async () => {
      const enabled = Boolean(els.obsSyncToggle.checked);
      if (obsBusy) {
        els.obsSyncToggle.checked = !enabled;
        return;
      }
      obsBusy = true;
      obsPendingState = null;
      let appliedFreshState = false;
      let httpStatus = 0;
      els.obsSyncToggle.disabled = true;
      if (els.obsRebuildBtn) els.obsRebuildBtn.disabled = true;
      try {
        const resp = await consolePost("/api/obsidian/config", { enabled });
        httpStatus = resp.status || 0;
        const res = await resp.json();
        if (res && res.obsidian) {
          renderObsidianUI(res.obsidian);
          appliedFreshState = true;
        }
        if (!resp.ok || !res || !res.ok || !res.obsidian) {
          throw new Error((res && res.error) || "切换失败");
        }
        showBanner("info", enabled ? "已开启 Obsidian 单向自动同步并完成检查！" : "已暂停 Obsidian 自动同步（已有笔记保留不动）。");
        scheduleHideBanner(3000);
      } catch (err) {
        if (!appliedFreshState) {
          els.obsSyncToggle.checked = !enabled;
          if (httpStatus === 0 || httpStatus >= 500) {
            obsOnline = false;
          }
        }
        showBanner("err", `切换 Obsidian 同步开关失败：${err.message}`);
      } finally {
        obsBusy = false;
        const pending = obsPendingState;
        obsPendingState = null;
        if (!appliedFreshState && pending) {
          renderObsidianUI(pending);
        }
        if (els.obsSyncToggle) els.obsSyncToggle.disabled = !obsOnline;
        if (els.obsRebuildBtn) els.obsRebuildBtn.disabled = !obsOnline;
      }
    });
  }

  if (els.obsRebuildBtn) {
    els.obsRebuildBtn.addEventListener("click", async () => {
      if (obsBusy) return;
      obsBusy = true;
      obsPendingState = null;
      let appliedFreshState = false;
      let httpStatus = 0;
      const oldLabel = els.obsRebuildBtn.textContent;
      els.obsRebuildBtn.disabled = true;
      if (els.obsSyncToggle) els.obsSyncToggle.disabled = true;
      els.obsRebuildBtn.textContent = "正在重建…";
      try {
        const resp = await consolePost("/api/obsidian/rebuild", "{}");
        httpStatus = resp.status || 0;
        const res = await resp.json();
        if (res && res.obsidian) {
          renderObsidianUI(res.obsidian);
          appliedFreshState = true;
        }
        if (!resp.ok || !res || !res.ok) {
          throw new Error((res && res.error) || "全量重建失败");
        }
        const r = res.result || {};
        showBanner(
          "info",
          `Obsidian 全量重建完成：写入 ${r.written || 0} 份，哈希未变跳过 ${r.skipped || 0} 份（耗时 ${r.duration_ms || 0} ms）`
        );
        scheduleHideBanner(3600);
      } catch (err) {
        if (!appliedFreshState && (httpStatus === 0 || httpStatus >= 500)) {
          obsOnline = false;
        }
        showBanner("err", `Obsidian 全量重建失败：${err.message}`);
      } finally {
        obsBusy = false;
        const pending = obsPendingState;
        obsPendingState = null;
        if (!appliedFreshState && pending) {
          renderObsidianUI(pending);
        }
        if (els.obsRebuildBtn) {
          els.obsRebuildBtn.disabled = !obsOnline;
          els.obsRebuildBtn.textContent = oldLabel;
        }
        if (els.obsSyncToggle) els.obsSyncToggle.disabled = !obsOnline;
      }
    });
  }

  function populateModelOptions(provider, selectedModel) {
    const list = PROVIDER_MODELS[provider] || PROVIDER_MODELS.DeepSeekLLM;
    els.modelSelect.innerHTML = "";
    for (const item of list) {
      const opt = document.createElement("option");
      opt.value = item.value;
      opt.textContent = item.label;
      els.modelSelect.appendChild(opt);
    }
    const vals = list.map(x => x.value);
    if (selectedModel && vals.includes(selectedModel)) {
      els.modelSelect.value = selectedModel;
      els.modelCustomInput.value = selectedModel;
    } else if (selectedModel) {
      els.modelSelect.value = "__custom__";
      els.modelCustomInput.value = selectedModel;
    } else {
      els.modelSelect.value = list[0].value;
      els.modelCustomInput.value = list[0].value;
    }
  }

  function checkDirty() {
    if (!savedConfig) return;
    els.charCount.textContent = `${els.promptInput.value.length} 字`;
    if (isSaving || isRestarting) return;
    const formDirty = (
      els.promptInput.value.trim() !== savedConfig.prompt ||
      els.providerSelect.value !== savedConfig.llm_provider ||
      els.modelCustomInput.value.trim() !== savedConfig.model_name ||
      els.voiceSelect.value !== savedConfig.voice
    );
    if (formDirty || serverVoiceDirty) {
      els.saveBtn.disabled = false;
      els.saveBtn.classList.add("has-dirty");
      els.saveBtnLabel.textContent = formDirty ? "保存并立即生效" : "立即恢复线上音色权重";
    } else {
      els.saveBtn.disabled = true;
      els.saveBtn.classList.remove("has-dirty");
      els.saveBtnLabel.textContent = "已同步生效";
    }
  }

  els.providerSelect.addEventListener("change", () => {
    populateModelOptions(els.providerSelect.value, null);
    checkDirty();
  });

  els.modelSelect.addEventListener("change", () => {
    if (els.modelSelect.value !== "__custom__") {
      els.modelCustomInput.value = els.modelSelect.value;
    } else {
      els.modelCustomInput.focus();
    }
    checkDirty();
  });

  els.modelCustomInput.addEventListener("input", () => {
    const list = PROVIDER_MODELS[els.providerSelect.value] || [];
    const vals = list.map(x => x.value);
    els.modelSelect.value = vals.includes(els.modelCustomInput.value.trim()) ? els.modelCustomInput.value.trim() : "__custom__";
    checkDirty();
  });

  let selectedMeiEmotion = "auto";
  const MEI_EMOTION_SAMPLES = {
    auto: "嗯，哥哥欢迎回来，今天累不累？我已经为你泡好了热茶。",
    gentle: "哥哥今天已经忙了一整天了，先喝口温水，剩下的事就交给我吧。",
    happy: "太棒啦哥哥！我就知道你一定能搞定的，今晚我做最拿手的料理给你庆祝一下吧！",
    playful: "呵呵，哥哥又想偷偷熬夜打游戏了吗？被我抓到了哦。",
    shy: "哥哥突然这么夸我，就算是我，也会觉得有些不好意思呢。",
    serious: "这件事可不能马虎，我已经帮你把关键步骤检查完了，放心去执行吧。",
    tender: "别难过了哥哥，不管发生什么事，我都会一直安安静静陪在你身边的。",
    calm: "今夜月色很美，嗯，正好适合放慢脚步，聊一聊今天发生的事。"
  };
  const KNOWN_PREVIEW_LINES = new Set([
    ...Object.values(MEI_EMOTION_SAMPLES),
    "舰长，欢迎回来。今天累不累？我已经为你准备好了热茶和晚餐。",
    "哈啰啊，我是小智啦，今天有什么好玩的事快跟我讲讲！"
  ]);

  function syncMeiEmotionVisibility() {
    if (els.meiEmotionBar) {
      els.meiEmotionBar.style.display = els.voiceSelect.value === "__gsv_raiden_mei__" ? "flex" : "none";
    }
  }

  els.promptInput.addEventListener("input", () => {
    els.presetBar.querySelectorAll(".preset-pill").forEach(b => b.classList.remove("active"));
    checkDirty();
  });

  els.voiceSelect.addEventListener("change", () => {
    syncMeiEmotionVisibility();
    checkDirty();
  });

  els.meiEmotionBar.addEventListener("click", (e) => {
    const btn = e.target.closest(".preset-pill");
    if (!btn) return;
    selectedMeiEmotion = btn.dataset.emotion || "auto";
    els.meiEmotionBar.querySelectorAll(".preset-pill").forEach(b => b.classList.toggle("active", b === btn));
    const curVal = els.previewTextInput.value.trim();
    if (!curVal || KNOWN_PREVIEW_LINES.has(curVal)) {
      els.previewTextInput.value = MEI_EMOTION_SAMPLES[selectedMeiEmotion] || MEI_EMOTION_SAMPLES.auto;
    }
  });

  els.presetBar.addEventListener("click", (e) => {
    const btn = e.target.closest(".preset-pill");
    if (!btn) return;
    const key = btn.dataset.preset;
    if (PRESETS[key]) {
      els.promptInput.value = PRESETS[key];
      if (key === "mei") {
        els.voiceSelect.value = "__gsv_raiden_mei__";
        els.previewTextInput.value = MEI_EMOTION_SAMPLES[selectedMeiEmotion] || MEI_EMOTION_SAMPLES.auto;
      } else if (key === "taiwan") {
        els.voiceSelect.value = "__gsv_xiaozhi_tw__";
        els.previewTextInput.value = "哈啰啊，我是小智啦，今天有什么好玩的事快跟我讲讲！";
      }
      syncMeiEmotionVisibility();
      els.presetBar.querySelectorAll(".preset-pill").forEach(b => b.classList.toggle("active", b === btn));
      checkDirty();
    }
  });

  els.copyOtaBtn.addEventListener("click", async () => {
    const txt = els.otaDisplay.textContent.trim();
    try {
      await navigator.clipboard.writeText(txt);
      showBanner("info", `已复制 OTA 接口地址：${txt}`);
      scheduleHideBanner(2600);
    } catch (_) {}
  });

  function switchMonitorTab(tab) {
    const isChat = tab === "chat";
    const isLogs = tab === "logs";
    els.tabChat.classList.toggle("active", isChat);
    els.tabLogs.classList.toggle("active", isLogs);
    els.chatStream.classList.toggle("hidden", !isChat);
    els.logStream.classList.toggle("active", isLogs);
    els.clearHistoryBtn.style.visibility = isChat ? "visible" : "hidden";
    if (isLogs) {
      els.logStream.scrollTop = els.logStream.scrollHeight;
    }
  }

  els.tabChat.addEventListener("click", () => switchMonitorTab("chat"));
  els.tabLogs.addEventListener("click", () => switchMonitorTab("logs"));

  els.clearHistoryBtn.addEventListener("click", async () => {
    await consolePost("/api/clear-history", "{}");
    lastChatJson = "";
    pollState();
  });

  els.previewBtn.addEventListener("click", async () => {
    if (!els.previewAudio.paused) {
      els.previewAudio.pause();
      els.previewAudio.currentTime = 0;
      els.previewBtnText.textContent = "试听音色";
      return;
    }
    els.previewBtn.disabled = true;
    els.previewBtnText.textContent = "合成中…";
    try {
      const resp = await consolePost("/api/tts-preview", {
        voice: els.voiceSelect.value,
        text: els.previewTextInput.value.trim(),
        emotion: selectedMeiEmotion
      });
      if (!resp.ok) {
        let errMsg = "音色试听失败，请确认本机语音合成服务运行正常。";
        try {
          const errJson = await resp.json();
          if (errJson && errJson.error) errMsg = errJson.error;
          if (errJson && errJson.rollback_warning) errMsg += `（${errJson.rollback_warning}）`;
        } catch (_) {}
        throw new Error(errMsg);
      }
      const previewWarn = resp.headers.get("X-Preview-Warning");
      if (previewWarn) {
        try { showBanner("warn", decodeURIComponent(previewWarn)); } catch (_) { showBanner("warn", previewWarn); }
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      els.previewAudio.src = url;
      els.previewBtn.disabled = false;
      els.previewBtnText.textContent = "停止播放";
      els.previewAudio.onended = () => {
        els.previewBtnText.textContent = "试听音色";
        URL.revokeObjectURL(url);
      };
      await els.previewAudio.play();
    } catch (err) {
      els.previewBtn.disabled = false;
      els.previewBtnText.textContent = "试听音色";
      showBanner("err", (err && err.message) ? err.message : "音色试听失败，请确认本机语音合成服务运行正常。");
    }
  });

  async function saveConfig() {
    if (els.saveBtn.disabled || isRestarting || isSaving) return;
    const payload = {
      prompt: els.promptInput.value.trim(),
      llm_provider: els.providerSelect.value,
      model_name: els.modelCustomInput.value.trim(),
      voice: els.voiceSelect.value,
      restart: true
    };
    if (!payload.prompt) {
      showBanner("err", "角色人设提示词不能为空。");
      return;
    }

    isSaving = true;
    els.saveBtn.disabled = true;
    els.saveBtn.classList.remove("has-dirty");
    els.saveBtnLabel.textContent = "正在应用配置…";

    try {
      const resp = await consolePost("/api/config", payload);
      const res = await resp.json();
      if (!res.ok) throw new Error(res.error || "保存失败");
      stateEpoch += 1;
      savedConfig = {
        prompt: payload.prompt,
        llm_provider: payload.llm_provider,
        model_name: payload.model_name,
        voice: payload.voice
      };
      if (res.restarting) {
        isSaving = false;
        restartTargetBootId = knownBootId || (typeof res.boot_id === "string" ? res.boot_id.trim() : "");
        isRestarting = true;
        els.saveBtnLabel.textContent = "正在重启生效…";
        els.statusPill.className = "status-pill restarting";
        els.statusText.textContent = "正在重新加载语音服务…";
        showBanner("warn", "新配置已写入，正在平滑重启语音服务并加载 MCP 工具（约需 8～12 秒）…");
        await new Promise(r => setTimeout(r, 2500));
      } else {
        isSaving = false;
        isRestarting = false;
        if (typeof res.voice_dirty === "boolean") {
          serverVoiceDirty = res.voice_dirty;
        } else if (res.voice_recovered) {
          serverVoiceDirty = false;
        }
        checkDirty();
        const changedCount = Array.isArray(res.changed_fields) ? res.changed_fields.length : 0;
        let okMsg = "配置无变化。";
        if (changedCount > 0 && res.voice_recovered) {
          okMsg = "新配置已热更新生效，并已立即恢复线上音色权重！";
        } else if (changedCount > 0) {
          okMsg = "新配置已热更新生效（免重启）！";
        } else if (res.voice_recovered) {
          okMsg = "已立即恢复线上音色权重！";
        }
        showBanner("info", okMsg);
        scheduleHideBanner(3600);
        pollState(true);
      }
    } catch (err) {
      isSaving = false;
      isRestarting = false;
      checkDirty();
      showBanner("err", `保存失败：${err.message}`);
    }
  }

  els.saveBtn.addEventListener("click", saveConfig);
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveConfig();
    }
  });

  function renderMessages(messages) {
    const jsonStr = JSON.stringify(messages);
    if (jsonStr === lastChatJson) return;
    lastChatJson = jsonStr;
    const nearBottom = (els.chatStream.scrollHeight - els.chatStream.scrollTop - els.chatStream.clientHeight) < 80;
    Array.from(els.chatStream.querySelectorAll(".msg-item")).forEach(n => n.remove());

    if (!messages || messages.length === 0) {
      els.emptyState.style.display = "block";
      return;
    }
    els.emptyState.style.display = "none";

    for (const m of messages) {
      const wrap = document.createElement("div");
      wrap.className = `msg-item ${m.role === "user" ? "user" : "assistant"}`;
      const meta = document.createElement("div");
      meta.className = "msg-meta num";
      meta.textContent = `${m.role === "user" ? "你（ESP32 麦克风）" : "小智 / 芽衣"} · ${m.time || ""}`;
      const bubble = document.createElement("div");
      bubble.className = "msg-bubble";
      bubble.textContent = m.text;
      wrap.appendChild(meta);
      wrap.appendChild(bubble);
      els.chatStream.appendChild(wrap);
    }
    if (nearBottom) els.chatStream.scrollTop = els.chatStream.scrollHeight;
  }

  function applyLogsDelta(logs, nextCursor, incremental, reset) {
    const incoming = Array.isArray(logs) ? logs : [];
    const hasCursor = typeof nextCursor === "number" && Number.isFinite(nextCursor);
    let changed = false;

    if (!incremental || reset || logCursor === null) {
      const nextStr = incoming.join("\n");
      if (nextStr !== logLines.join("\n") || reset) {
        logLines = incoming.slice(-150);
        changed = true;
      }
    } else if (incoming.length > 0) {
      logLines = logLines.concat(incoming);
      if (logLines.length > 150) {
        logLines = logLines.slice(-150);
      }
      changed = true;
    }

    if (hasCursor) {
      logCursor = nextCursor;
    }
    if (!changed) return;
    const nearBottom = (els.logStream.scrollHeight - els.logStream.scrollTop - els.logStream.clientHeight) < 80;
    els.logStream.textContent = logLines.join("\n");
    if (nearBottom) els.logStream.scrollTop = els.logStream.scrollHeight;
  }

  let pollTimer = null;
  let pollInFlight = false;

  async function pollState(forceFollowUp = false) {
    if (pollInFlight) {
      if (forceFollowUp) pollPendingAfterInFlight = true;
      return;
    }
    pollInFlight = true;
    const reqEpoch = stateEpoch;
    try {
      const stateUrl = (logCursor !== null) ? `/api/state?log_cursor=${encodeURIComponent(logCursor)}` : "/api/state";
      const resp = await fetch(stateUrl, { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      const cfg = data.config || {};
      const dev = data.device || {};

      if (!savedConfig) {
        savedConfig = {
          prompt: cfg.prompt || "",
          llm_provider: cfg.llm_provider || "DeepSeekLLM",
          model_name: cfg.model_name || "deepseek-flash",
          voice: cfg.voice || "__gsv_raiden_mei__"
        };
        els.providerSelect.value = savedConfig.llm_provider;
        populateModelOptions(savedConfig.llm_provider, savedConfig.model_name);
        els.promptInput.value = savedConfig.prompt;
        els.voiceSelect.value = savedConfig.voice;
        syncMeiEmotionVisibility();
        els.charCount.textContent = `${els.promptInput.value.length} 字`;
      }

      renderModeButtons(cfg.pc_mode || "safe");
      renderAudioOutButtons(cfg.audio_output || "esp32");
      if (!volDragging && els.pcAudioVolRange && typeof cfg.pc_audio_volume === "number") {
        const pct = Math.round(cfg.pc_audio_volume * 100);
        els.pcAudioVolRange.value = String(pct);
        els.pcAudioVolVal.textContent = `${pct}%`;
      }
      if (els.pcAudioStatusText) {
        els.pcAudioStatusText.textContent = cfg.pc_audio_online
          ? "已在线（48kHz WinMM 直通就绪）"
          : "离线（请运行 启动或重启服务.bat 拉起 18900）";
        els.pcAudioStatusText.style.color = cfg.pc_audio_online ? "var(--accent)" : "var(--danger)";
      }
      if (!replyLenDragging && cfg.reply_length) {
        renderReplyLengthUI(cfg.reply_length);
      }
      if (cfg.idle_farewell) {
        renderIdleFarewellUI(cfg.idle_farewell);
      }
      if (!relDragging && !relSaving && cfg.relationship) {
        renderRelationshipUI(cfg.relationship);
      }
      if (cfg.anima_outbox) {
        renderAnimaOutboxUI(cfg.anima_outbox);
      }
      if (cfg.vad_speculative || cfg.vad_speculative_stats) {
        renderVadSpeculativeUI(cfg.vad_speculative, cfg.vad_speculative_stats);
      }
      if (cfg.asr_corrections) {
        renderAsrCorrectionsUI(cfg.asr_corrections);
      }
      if (els.animaBackupText && cfg.anima_backup) {
        const bk = cfg.anima_backup;
        if (bk.last_error) {
          els.animaBackupText.textContent = `备份异常 (${bk.last_error})`;
          els.animaBackupText.style.color = "var(--danger)";
        } else if (bk.last_backup_at) {
          const okTag = bk.integrity_ok === true ? "已校验" : (bk.integrity_ok === false ? "校验异常" : "待校验");
          els.animaBackupText.textContent = `${bk.last_backup_at} (${okTag} · 日${bk.daily_count || 0}/周${bk.weekly_count || 0})`;
          els.animaBackupText.style.color = bk.integrity_ok === false ? "var(--danger)" : "var(--accent)";
        } else if (bk.probed === false) {
          els.animaBackupText.textContent = "检测中…";
          els.animaBackupText.style.color = "var(--ink-muted)";
        } else {
          els.animaBackupText.textContent = "等待首次每日备份";
          els.animaBackupText.style.color = "var(--ink-muted)";
        }
      }
      if (reqEpoch === stateEpoch) {
        const prevVoiceDirty = serverVoiceDirty;
        serverVoiceDirty = Boolean(cfg.voice_dirty);
        if (prevVoiceDirty !== serverVoiceDirty) {
          checkDirty();
        }
        if (cfg.voice_dirty) {
          if (els.banner && (!els.banner.classList.contains("show") || els.banner.dataset.source === "voice_dirty")) {
            showBanner("warn", `线上音色权重待恢复（后台探测自动重试中）${cfg.voice_dirty_error ? "：" + cfg.voice_dirty_error : ""}`);
            els.banner.dataset.source = "voice_dirty";
          }
        } else if (els.banner && els.banner.dataset.source === "voice_dirty") {
          hideBanner();
        }
      }
      if (cfg.obsidian) {
        if (obsBusy) {
          obsOnline = Boolean(cfg.obsidian.online);
          obsPendingState = cfg.obsidian;
        } else {
          obsPendingState = null;
          renderObsidianUI(cfg.obsidian);
        }
      }

      els.dsKeyBadge.textContent = cfg.deepseek_key_ok ? "已就绪" : "未设置";
      els.dsKeyBadge.style.color = cfg.deepseek_key_ok ? "var(--accent)" : "var(--danger)";
      els.gemKeyBadge.textContent = cfg.gemini_key_ok ? "已就绪" : "未设置";
      els.gemKeyBadge.style.color = cfg.gemini_key_ok ? "var(--accent)" : "var(--danger)";
      if (els.llmCacheRateBadge && cfg.llm_cache) {
        const lc = cfg.llm_cache;
        const rate = (typeof lc.recent_hit_rate_pct === "number")
          ? lc.recent_hit_rate_pct
          : lc.overall_hit_rate_pct;
        if (typeof rate === "number" && lc.turns > 0) {
          const ttftTxt = (typeof lc.last_ttft_ms === "number") ? ` · 首字 ${Math.round(lc.last_ttft_ms)}ms` : "";
          els.llmCacheRateBadge.textContent = `${rate.toFixed(1)}% (${lc.turns}轮${ttftTxt} · ${lc.mode || "split"})`;
          els.llmCacheRateBadge.style.color = rate >= 70 ? "var(--accent)" : "var(--warn)";
        } else {
          els.llmCacheRateBadge.textContent = `待对话 (${lc.mode || "split"})`;
          els.llmCacheRateBadge.style.color = "var(--ink-muted)";
        }
      }

      if (!l3Saving) {
        renderL3BackendUI(cfg.l3_backend, cfg.agy_bridge_online);
      }

      els.otaDisplay.textContent = cfg.ota_url || "http://localhost:18003/xiaozhi/ota/";
      els.deviceIdText.textContent = `设备 MAC: ${dev.device_id || "--"}`;
      els.lastSeenText.textContent = `最近活跃: ${dev.last_seen || "等待唤醒"}`;

      const incomingBootId = typeof data.boot_id === "string" ? data.boot_id.trim() : "";
      let tokenRefreshed = false;
      if (incomingBootId) {
        if (knownBootId && incomingBootId !== knownBootId) {
          knownBootId = incomingBootId;
          await refreshConsoleToken();
          tokenRefreshed = true;
        } else if (!knownBootId) {
          knownBootId = incomingBootId;
        }
      }

      if (isRestarting && dev.server_ready) {
        const bootChanged = !restartTargetBootId || (incomingBootId && incomingBootId !== restartTargetBootId);
        if (bootChanged) {
          isRestarting = false;
          restartTargetBootId = "";
          if (!tokenRefreshed) {
            await refreshConsoleToken();
          }
          checkDirty();
          showBanner("info", "新配置已成功生效！");
          scheduleHideBanner(3600);
        }
      }

      const hlth = data.health || {};
      if (els.securityBanner) {
        const secFailed = (hlth.security_ok === false) || Boolean(hlth.insecure_boot);
        const envArmed = Boolean(hlth.insecure_boot_env);
        if (secFailed) {
          const fails = Array.isArray(hlth.security_failures) ? hlth.security_failures.join(", ") : "";
          els.securityBanner.className = "security-banner show";
          els.securityBannerText.textContent =
            `安全告警：当前处于不安全降级模式 (XIAOZHI_ALLOW_INSECURE_BOOT=1)，以下安全补丁未生效：${fails || "请查看 /api/health"}`;
          els.securityBannerRight.textContent = "HTTP 503";
        } else if (envArmed) {
          els.securityBanner.className = "security-banner show";
          els.securityBannerText.textContent =
            "安全提醒：应急开关 XIAOZHI_ALLOW_INSECURE_BOOT=1 已开启，建议排障完成后立即移除以恢复 fail-closed 保护。";
          els.securityBannerRight.textContent = "OVERRIDE ARMED";
        } else {
          els.securityBanner.className = "security-banner";
        }
      }

      if (!isRestarting) {
        const degList = Array.isArray(hlth.degraded_list)
          ? hlth.degraded_list
          : (Array.isArray(hlth.degraded) ? hlth.degraded : []);
        const isDegraded = degList.length > 0 || (typeof hlth.degraded === "boolean" && hlth.degraded);
        if (hlth.security_ok === false) {
          els.statusPill.className = "status-pill restarting";
          els.statusText.textContent = "安全补丁异常（不安全降级模式）";
        } else if (isDegraded) {
          els.statusPill.className = "status-pill restarting";
          els.statusText.textContent = degList.length
            ? `补丁降级：${degList.join(", ")}`
            : "补丁契约降级（查看 /api/health）";
        } else if (!dev.server_ready) {
          els.statusPill.className = "status-pill restarting";
          els.statusText.textContent = "正在加载语音识别模型…";
        } else if (dev.activity === "listening") {
          els.statusPill.className = "status-pill listening";
          els.statusText.textContent = "ESP32 正在聆听说话…";
        } else if (dev.activity === "speaking") {
          els.statusPill.className = "status-pill listening";
          els.statusText.textContent = "正在语音回复 / 执行指令…";
        } else {
          els.statusPill.className = "status-pill";
          els.statusText.textContent = `服务运行就绪 · ${cfg.llm_provider === "GeminiOpenAI" ? "Gemini" : "DeepSeek"}`;
        }
      }

      renderMessages(data.messages || []);
      applyLogsDelta(
        data.logs || [],
        data.log_cursor,
        Boolean(data.logs_incremental),
        Boolean(data.logs_reset)
      );
    } catch (_) {
      obsOnline = false;
      if (!obsBusy) {
        if (els.obsSyncToggle) els.obsSyncToggle.disabled = true;
        if (els.obsRebuildBtn) els.obsRebuildBtn.disabled = true;
      }
      if (!isRestarting) {
        els.statusPill.className = "status-pill offline";
        els.statusText.textContent = "本地服务连接中断";
      }
    } finally {
      pollInFlight = false;
      if (pollPendingAfterInFlight) {
        pollPendingAfterInFlight = false;
        if (isTabVisible()) pollState();
      }
    }
  }

  function isTabVisible() {
    if (typeof document.visibilityState === "string") {
      return document.visibilityState === "visible" && !document.hidden;
    }
    return !document.hidden;
  }

  function startPolling() {
    if (!isTabVisible()) return;
    if (pollTimer === null) {
      pollTimer = setInterval(() => {
        if (isTabVisible()) pollState();
      }, 1200);
    }
    pollState(true);
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || document.hidden) {
      stopPolling();
    } else {
      startPolling();
    }
  });

  if (isTabVisible()) {
    startPolling();
  }
})();
