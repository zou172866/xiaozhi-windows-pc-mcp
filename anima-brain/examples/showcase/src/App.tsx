import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  ArrowUpRight,
  Mic,
  Phone,
  Video,
  Square,
  Check,
  ChevronDown,
  Copy,
  Download,
  ImagePlus,
  Layers3,
  LockKeyhole,
  Menu,
  Plus,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Volume2,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  downloadJson,
  errorText,
  fileData,
  initialToken,
  portrait,
  type Bootstrap,
  type Character,
  type ChatResult,
  type Conversation,
  type Inspection,
  type MemoryPage,
} from "./api";
import {
  Avatar,
  Busy,
  CharacterForm,
  ErrorNote,
  ProviderSettings,
  Sheet,
} from "./components";
import { MemoryPanel, TurnInspector } from "./memory";
import { useVoiceRecorder } from "./useVoiceRecorder";
import { CallOverlay } from "./CallOverlay";
import { ReplyImages } from "./images";

type Panel =
  "" | "settings" | "create" | "edit" | "memory" | "trace" | "characters";
type Attachment = { name: string; data: string; url: string };
type Artifact = { artifact: string; mime?: string };
const starters = [
  {
    label: "A little catch-up",
    text: "It’s been a long day. Can we just talk for a bit?",
  },
  {
    label: "Something to remember",
    text: "Remember that I prefer quiet evenings to big parties.",
  },
  {
    label: "Pick up where we left off",
    text: "What do you remember about me so far?",
  },
];

export default function App() {
  const [token, setToken] = useState(initialToken),
    [boot, setBoot] = useState<Bootstrap | null>(null);
  const [selected, setSelected] = useState(
      () => sessionStorage.getItem("companion-character") || "",
    ),
    [conversation, setConversation] = useState<Conversation | null>(null);
  const [panel, setPanel] = useState<Panel>(""),
    [traceId, setTraceId] = useState("");
  const [error, setError] = useState(""),
    [toast, setToast] = useState(""),
    [authFailed, setAuthFailed] = useState(false);
  const [loading, setLoading] = useState(false),
    [working, setBusy] = useState(false),
    [sending, setSending] = useState("");
  const [draft, setDraft] = useState<Inspection | null>(null),
    [message, setMessage] = useState(""),
    [memoryCount, setMemoryCount] = useState(0);
  const [attachments, setAttachments] = useState<Attachment[]>([]),
    [audioUrl, setAudioUrl] = useState("");
  const [moreHistory, setMoreHistory] = useState(false),
    [atBottom, setAtBottom] = useState(true);
  const bottom = useRef<HTMLDivElement>(null),
    scroll = useRef<HTMLDivElement>(null),
    composer = useRef<HTMLTextAreaElement>(null);
  const imageInput = useRef<HTMLInputElement>(null);
  const [transcribing, setTranscribing] = useState(false);
  const [providerTab, setProviderTab] = useState("chat");
  const urls = useRef(new Set<string>()),
    reduceMotion = useReducedMotion();
  const character = boot?.characters.find((c) => c.id === selected);
  const recorder = useVoiceRecorder({
    scope: selected,
    onAudio: transcribe,
    onError: setError,
    onNotice: setToast,
  });
  const [callMode, setCallMode] = useState<"voice" | "video" | null>(null);
  const busy = working || recorder.active || !!callMode;
  const tokenRef = useRef(token);
  tokenRef.current = token;
  function track(url: string) {
    urls.current.add(url);
    return url;
  }
  function release(url: string) {
    URL.revokeObjectURL(url);
    urls.current.delete(url);
  }
  useEffect(
    () => () => {
      urls.current.forEach(URL.revokeObjectURL);
    },
    [],
  );
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(""), 4500);
      return () => clearTimeout(timer);
    }
  }, [toast]);
  const refreshBoot = useCallback(async () => {
    const value = await api<Bootstrap>(tokenRef.current, "bootstrap");
    setBoot(value);
    return value;
  }, []);
  useEffect(() => {
    let active = true;
    if (!token) return;
    setAuthFailed(false);
    setError("");
    api<Bootstrap>(token, "bootstrap")
      .then((value) => {
        if (active) {
          setBoot(value);
          setSelected((s) =>
            value.characters.some((c) => c.id === s)
              ? s
              : value.characters[0]?.id || "",
          );
        }
      })
      .catch((e) => {
        if (active) {
          setError(errorText(e));
          setAuthFailed(e instanceof ApiError && e.status === 401);
        }
      });
    return () => {
      active = false;
    };
  }, [token]);
  const loadConversation = useCallback(async (id: string) => {
    const [value, memories] = await Promise.all([
      api<Conversation>(tokenRef.current, "conversation", undefined, {
        character_id: id,
      }),
      api<MemoryPage>(tokenRef.current, "memory_panel", undefined, {
        character_id: id,
      }),
    ]);
    const pending = value.pending
      ? await api<Inspection>(tokenRef.current, "turn", undefined, {
          character_id: id,
          turn_id: value.pending.id,
        })
      : null;
    return { value, pending, count: memories.total };
  }, []);
  useEffect(() => {
    if (!selected) return;
    sessionStorage.setItem("companion-character", selected);
    let active = true;
    setLoading(true);
    setConversation(null);
    setDraft(null);
    setMemoryCount(0);
    setMessage("");
    setError("");
    setAttachments((items) => {
      items.forEach((x) => release(x.url));
      return [];
    });
    setAudioUrl("");
    loadConversation(selected)
      .then(({ value, pending, count }) => {
        if (active) {
          setConversation(value);
          setDraft(pending);
          setMemoryCount(count);
          setMoreHistory(value.turns.length === 50);
        }
      })
      .catch((e) => {
        if (active) setError(errorText(e));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selected, loadConversation]);
  useEffect(() => {
    if (atBottom && scroll.current)
      scroll.current.scrollTo({
        top: scroll.current.scrollHeight,
        behavior: "instant",
      });
  }, [conversation?.turns.length, sending, draft, atBottom, reduceMotion]);
  useEffect(() => {
    if (composer.current) {
      composer.current.style.height = "auto";
      composer.current.style.height =
        Math.min(composer.current.scrollHeight, 150) + "px";
    }
  }, [message]);
  async function reload() {
    const { value, pending, count } = await loadConversation(selected);
    setConversation(value);
    setDraft(pending);
    setMemoryCount(count);
  }
  function inspect(id: string) {
    setTraceId(id);
    setPanel("trace");
  }
  async function send(event?: React.FormEvent) {
    event?.preventDefault();
    if (!message.trim() || busy || !character || conversation?.pending) return;
    if (!boot?.capabilities.chat) {
      setProviderTab("chat");
      setPanel("settings");
      return;
    }
    const text = message.trim(),
      input = attachments.map((a) => ({ data: a.data }));
    setBusy(true);
    setSending(text);
    setError("");
    setAtBottom(true);
    setMessage("");
    setAttachments([]);
    let restored = false;
    try {
      await api<ChatResult>(token, "chat", {
        character_id: selected,
        message: text,
        request_id: crypto.randomUUID(),
        attachments: input,
      });
      await reload();
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status < 500 && e.status !== 409) {
        setMessage(text);
        setAttachments(attachments);
        restored = true;
      }
      try {
        await reload();
      } catch {
        /* Preserve the original failure. */
      }
    } finally {
      if (!restored) attachments.forEach((a) => release(a.url));
      setBusy(false);
      setSending("");
      composer.current?.focus();
    }
  }
  async function resolvePending(retain: boolean | null) {
    if (!conversation?.pending) return;
    setBusy(true);
    setError("");
    try {
      await api(token, retain === null ? "abort" : "approve", {
        character_id: selected,
        turn_id: conversation.pending.id,
        retain,
      });
      await reload();
      setToast(
        retain === null
          ? "Unfinished turn discarded."
          : retain
            ? "Reply and memories saved."
            : "Reply saved without new memories.",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function loadOlder() {
    if (!conversation?.turns.length) return;
    setLoading(true);
    const height = scroll.current?.scrollHeight || 0;
    try {
      const result = await api<{ turns: Conversation["turns"] }>(
        token,
        "history",
        undefined,
        {
          character_id: selected,
          before: String(conversation.turns[0].cursor),
        },
      );
      setConversation({
        ...conversation,
        turns: [...result.turns, ...conversation.turns],
      });
      setMoreHistory(result.turns.length === 50);
      requestAnimationFrame(() => {
        if (scroll.current)
          scroll.current.scrollTop += scroll.current.scrollHeight - height;
      });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }
  function capability(key: string, action: () => void) {
    if (boot?.capabilities[key]) action();
    else {
      setProviderTab(key);
      setPanel("settings");
      setToast("Connect a " + key.replace("_", " ") + " provider to use this.");
    }
  }
  async function attach(files: FileList | null) {
    if (!files) return;
    setError("");
    const added: Attachment[] = [];
    try {
      if (files.length + attachments.length > 4)
        throw new Error("You can attach up to four images.");
      if (
        [...files].reduce((n, f) => n + f.size, 0) +
          attachments.reduce((n, f) => n + f.data.length * 0.75, 0) >
        20 * 1024 * 1024
      )
        throw new Error("Keep combined images below 20 MB.");
      for (const file of files) {
        if (file.size > 10 * 1024 * 1024)
          throw new Error("Each image must be smaller than 10 MB.");
        if (!["image/jpeg", "image/png", "image/webp"].includes(file.type))
          throw new Error("Choose a PNG, JPEG or WebP image.");
        added.push({
          name: file.name,
          data: await fileData(file),
          url: track(URL.createObjectURL(file)),
        });
      }
      setAttachments((a) => [...a, ...added]);
    } catch (e) {
      added.forEach((a) => release(a.url));
      setError(errorText(e));
    }
    if (imageInput.current) imageInput.current.value = "";
  }
  async function transcribe(file: File) {
    setBusy(true);
    setTranscribing(true);
    setError("");
    try {
      const result = await api<{ text: string }>(token, "transcribe", {
        character_id: selected,
        audio: await fileData(file),
        mime: file.type,
        request_id: crypto.randomUUID(),
      });
      if (!result.text.trim()) {
        setToast("No speech was detected. Try recording again.");
        return;
      }
      setMessage((m) => m + (m ? "\n" : "") + result.text);
      setToast("Transcribed. Review the text before sending.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setTranscribing(false);
      setBusy(false);
      requestAnimationFrame(() => composer.current?.focus());
    }
  }
  async function artifactUrl(artifact: string) {
    const response = await fetch(
      "/media/" +
        encodeURIComponent(artifact) +
        "?" +
        new URLSearchParams({ character_id: selected }),
      { headers: { Authorization: "Bearer " + token } },
    );
    if (!response.ok) throw new Error("Could not load the saved media.");
    return track(URL.createObjectURL(await response.blob()));
  }
  async function speak(text: string) {
    if (!boot?.capabilities.speech) {
      setProviderTab("speech");
      setPanel("settings");
      setToast("Configure a voice provider and character voice ID.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await api<Artifact>(token, "speech", {
        character_id: selected,
        text,
        request_id: crypto.randomUUID(),
      });
      const url = await artifactUrl(result.artifact);
      if (audioUrl) release(audioUrl);
      setAudioUrl(url);
      setToast("Audio is ready to play.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function exportCharacter() {
    try {
      const data = await api(token, "export", undefined, {
        character_id: selected,
      });
      downloadJson(data, (character?.name || "companion") + "-export.json");
      setToast("Character data exported.");
    } catch (e) {
      setError(errorText(e));
    }
  }
  async function saveCharacter(c: Character) {
    await refreshBoot();
    setSelected(c.id);
    setToast("Character saved.");
  }

  if (!token || authFailed)
    return (
      <div className="connection-screen">
        <img
          className="connection-mark"
          src="/portraits/anima-icon.png"
          alt="Anima"
        />
        <span className="eyebrow">COMPANION STUDIO</span>
        <h1>A space of your own.</h1>
        <p>
          Open the private link printed by the local server to connect to your
          companions.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const value = String(
              new FormData(e.currentTarget).get("token") || "",
            ).trim();
            sessionStorage.setItem("companion-token", value);
            setToken(value);
          }}
        >
          <label>
            Local access token
            <input
              name="token"
              type="password"
              required
              minLength={24}
              autoComplete="off"
              placeholder="Paste the token from your terminal"
            />
          </label>
          <button className="primary">
            Open studio <ArrowRight size={16} />
          </button>
        </form>
        <ErrorNote message={error} />
        <small>
          The token stays in this browser tab. Your provider API key belongs in
          server settings.
        </small>
      </div>
    );
  if (!boot)
    return (
      <div className="connection-screen">
        <img
          className="connection-mark"
          src="/portraits/anima-icon.png"
          alt="Anima"
        />
        {error ? (
          <>
            <ErrorNote message={error} />
            <button className="secondary" onClick={() => location.reload()}>
              Reconnect
            </button>
          </>
        ) : (
          <Busy text="Opening your studio…" />
        )}
      </div>
    );
  return (
    <div className="studio">
      <header className="topbar">
        <a
          className="brand"
          href="https://anima-lab.io"
          target="_blank"
          rel="noreferrer"
          aria-label="Visit Anima Lab"
        >
          <img src="/portraits/anima-icon.png" alt="" />
          <span>
            anima<span className="brand-dot">.</span>
          </span>
        </a>
        <div className="studio-label">
          <span className="header-divider" />
          COMPANION STUDIO<span className="edition">PYTHON EDITION</span>
        </div>
        <nav className="top-actions" aria-label="Studio navigation">
          <a
            className="visit-anima"
            href="https://anima-lab.io"
            target="_blank"
            rel="noreferrer"
          >
            Explore Anima <ArrowUpRight size={14} />
          </a>
          <button
            className="icon-button"
            onClick={() => {
              setProviderTab("chat");
              setPanel("settings");
            }}
            aria-label="Provider settings"
            disabled={busy}
            title="Provider settings"
          >
            <Settings2 size={19} />
          </button>
        </nav>
      </header>
      {boot.preview_mode && (
        <div className="preview-banner">
          <span className="status-dot" />
          Offline preview · scripted replies, real local storage. No model
          calls.
        </div>
      )}
      <div className="workspace">
        <aside className="character-column">
          <div className="column-heading">
            <span className="eyebrow">YOUR COMPANIONS</span>
            <button
              className="icon-button"
              aria-label="Create a character"
              onClick={() => setPanel("create")}
              disabled={busy}
            >
              <Plus size={18} />
            </button>
          </div>
          <div className="character-switcher" aria-label="Choose a companion">
            {boot.characters.map((c) => (
              <button
                key={c.id}
                disabled={busy}
                className={
                  "character-tab " + (selected === c.id ? "active" : "")
                }
                aria-label={"Chat with " + c.name}
                aria-pressed={selected === c.id}
                onClick={() => setSelected(c.id)}
              >
                <Avatar character={c} />
                <span>{c.name}</span>
              </button>
            ))}
            {!boot.characters.length && (
              <button
                className="text-button"
                onClick={() => setPanel("create")}
              >
                <Plus size={15} />
                Create your first companion
              </button>
            )}
          </div>
          <AnimatePresence mode="wait">
            <motion.div
              key={selected || "welcome"}
              className="character-profile"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -5 }}
              transition={{ duration: 0.22 }}
            >
              <div
                className={
                  "portrait-frame " +
                  (!portrait(selected) ? "portrait-abstract" : "")
                }
              >
                {portrait(selected) ? (
                  <img
                    className="character-portrait"
                    src={portrait(selected)!}
                    alt={character?.name + " character portrait"}
                  />
                ) : (
                  <div className="portrait-monogram">
                    {character?.name.slice(0, 1) || "a"}
                    <span>someone to talk to.</span>
                  </div>
                )}
                <div className="portrait-index">
                  <span>EST. IN YOUR WORLD</span>
                  <span>
                    0
                    {Math.max(
                      1,
                      boot.characters.findIndex((c) => c.id === selected) + 1,
                    )}
                  </span>
                </div>
              </div>
              <div className="profile-title">
                <h1>
                  {character?.name || "Your story starts here."}
                  <span className="coral-dot">.</span>
                </h1>
                {character && (
                  <button
                    className="icon-button"
                    aria-label="Edit character"
                    onClick={() => setPanel("edit")}
                    disabled={busy}
                  >
                    <SlidersHorizontal size={17} />
                  </button>
                )}
              </div>
              <p className="persona-excerpt">
                {character?.persona ||
                  "Give your companion a voice, a personality, and a little room to grow."}
              </p>
              {character && (
                <div className="profile-tags">
                  <span>
                    <Layers3 size={12} />
                    {character.memory_policy === "off"
                      ? "Memory saving off"
                      : character.memory_policy === "review"
                        ? "Memory on your terms"
                        : "Remembers the details"}
                  </span>
                  <span>
                    <LockKeyhole size={12} />
                    Stored locally
                  </span>
                </div>
              )}
            </motion.div>
          </AnimatePresence>
          <div className="sidebar-footer">
            <p>
              A familiar feeling.
              <br />A brain you can make your own.
            </p>
            <a
              href="https://anima-lab.io/download"
              target="_blank"
              rel="noreferrer"
            >
              Try Anima on your phone <ArrowUpRight size={13} />
            </a>
          </div>
        </aside>
        <main className="conversation-pane" id="conversation">
          <div className="chat-header">
            <div className="chat-identity">
              <button
                className="icon-button mobile-characters"
                aria-label="Choose companion"
                onClick={() => setPanel("characters")}
                disabled={busy}
              >
                <Menu size={19} />
              </button>
              {character && (
                <span className="mobile-avatar">
                  <Avatar character={character} />
                </span>
              )}
              <div>
                <span className="chat-name">
                  {character?.name || "Your companion"}
                  <ChevronDown size={13} className="mobile-only" />
                </span>
                <span className="connection-status">
                  <i
                    className={
                      "status-dot " + (boot.capabilities.chat ? "ready" : "")
                    }
                  />
                  {boot.preview_mode
                    ? "Offline preview"
                    : boot.capabilities.chat
                      ? "Model configured"
                      : "Connect your model"}
                </span>
              </div>
            </div>
            <div className="chat-header-actions">
              {(["voice", "video"] as const).map((mode) => (
                <button
                  key={mode}
                  className="icon-button call-entry"
                  aria-label={
                    mode === "voice" ? "Start voice call" : "Start video call"
                  }
                  title={mode === "voice" ? "Voice call" : "Video call"}
                  disabled={!character || busy || !!draft}
                  onClick={() => {
                    if (!boot.capabilities[mode + "_call"]) {
                      setProviderTab("calls");
                      setPanel("settings");
                      return;
                    }
                    if (!character?.voice_id) {
                      setPanel("edit");
                      setToast("Set this companion’s voice ID first.");
                      return;
                    }
                    if (
                      mode === "video" &&
                      !boot.appearance_references?.[selected]
                    ) {
                      setPanel("edit");
                      setToast(
                        "Upload an appearance reference for video calls.",
                      );
                      return;
                    }
                    setCallMode(mode);
                  }}
                >
                  {mode === "voice" ? <Phone size={17} /> : <Video size={19} />}
                </button>
              ))}
              <button
                className="memory-button"
                aria-label="Memories"
                onClick={() => setPanel("memory")}
                disabled={!character}
              >
                <Layers3 size={16} />
                <span>Memories</span>
                <b>{memoryCount}</b>
              </button>
              <button
                className="icon-button"
                aria-label="Export character data"
                title="Export character data"
                disabled={!character || busy}
                onClick={exportCharacter}
              >
                <Download size={17} />
              </button>
            </div>
          </div>
          <div
            className="chat-scroll"
            ref={scroll}
            onScroll={() => {
              const el = scroll.current;
              if (el)
                setAtBottom(
                  el.scrollHeight - el.scrollTop - el.clientHeight < 100,
                );
            }}
          >
            {loading && !conversation && (
              <div className="welcome">
                <Busy text="Opening the conversation…" />
              </div>
            )}
            {!loading &&
              !conversation?.turns.length &&
              !sending &&
              !conversation?.pending && (
                <motion.div
                  className="welcome"
                  initial={{ opacity: 0, y: 14 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.5 }}
                >
                  <div className="welcome-mark">
                    <Sparkles size={24} strokeWidth={1.2} />
                  </div>
                  <span className="eyebrow">A LITTLE MORE THAN HELLO</span>
                  <h2>
                    Some things are
                    <br />
                    worth <em>remembering.</em>
                  </h2>
                  <p>
                    A small thought. A long day. That thing you haven’t
                    <br className="desktop-break" /> told anyone yet. There’s
                    room for it here.
                  </p>
                  {character ? (
                    <div className="starter-list">
                      {starters.map((s, i) => (
                        <button
                          key={s.label}
                          onClick={() => {
                            setMessage(s.text);
                            composer.current?.focus();
                          }}
                          disabled={busy || loading}
                        >
                          <span className="starter-number">0{i + 1}</span>
                          <span>{s.label}</span>
                          <ArrowUpRight size={16} />
                        </button>
                      ))}
                    </div>
                  ) : (
                    <button
                      className="primary"
                      onClick={() => setPanel("create")}
                    >
                      Create your companion <ArrowRight size={17} />
                    </button>
                  )}
                </motion.div>
              )}
            {!!conversation?.turns.length && (
              <>
                <div className="history-heading">
                  <span />
                  YOUR CONVERSATION
                  <span />
                </div>
                {moreHistory && (
                  <button
                    className="load-history text-button"
                    disabled={loading}
                    onClick={loadOlder}
                  >
                    {loading ? "Loading…" : "Load earlier messages"}
                  </button>
                )}
              </>
            )}
            <div
              className="messages"
              role="log"
              aria-live="polite"
              aria-relevant="additions text"
              aria-label="Conversation"
            >
              {conversation?.turns.map((turn) => (
                <div className="exchange" key={turn.id}>
                  <div className="message user-message">
                    <span className="message-label">YOU</span>
                    <p>{turn.user_message}</p>
                  </div>
                  <div className="message companion-message">
                    <span className="message-label">
                      {character?.name.toUpperCase()}
                    </span>
                    <p>{turn.assistant_reply}</p>
                    <ReplyImages
                      token={token}
                      characterId={selected}
                      result={turn.image_result}
                      onAppearance={() => setPanel("edit")}
                    />
                    <div className="reply-actions">
                      <button
                        aria-label="Inspect reply context"
                        title="Inspect saved context"
                        onClick={() => inspect(turn.id)}
                      >
                        <Layers3 size={14} />
                        Context
                      </button>
                      <button
                        aria-label="Copy reply"
                        title="Copy reply"
                        onClick={() =>
                          navigator.clipboard
                            .writeText(turn.assistant_reply)
                            .then(() => setToast("Reply copied."))
                            .catch(() =>
                              setToast("Clipboard access is unavailable."),
                            )
                        }
                      >
                        <Copy size={14} />
                      </button>
                      <button
                        aria-label="Read reply aloud"
                        title="Generate voice audio"
                        disabled={busy}
                        onClick={() => speak(turn.assistant_reply)}
                      >
                        <Volume2 size={15} />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
              {(sending || conversation?.pending) && (
                <div className="exchange">
                  <div className="message user-message">
                    <span className="message-label">YOU</span>
                    <p>{sending || conversation?.pending?.user_message}</p>
                  </div>
                  <div className="message companion-message">
                    <span className="message-label">
                      {character?.name.toUpperCase()}
                    </span>
                    {sending ? (
                      <div
                        className="thinking"
                        role="status"
                        aria-label="Waiting for the model"
                      >
                        <i />
                        <i />
                        <i />
                      </div>
                    ) : draft?.reply ? (
                      <>
                        <p>{draft.reply}</p>
                        <ReplyImages
                          token={token}
                          characterId={selected}
                          result={draft.image_result}
                          onAppearance={() => setPanel("edit")}
                        />
                        <div className="review-box">
                          <span className="eyebrow">READY FOR YOUR REVIEW</span>
                          {draft.proposals.memories.map((m, i) => (
                            <p key={i}>{m.text}</p>
                          ))}
                          {!!draft.proposals.corrections.length && (
                            <p>
                              {draft.proposals.corrections.length} memory
                              correction(s) proposed.
                            </p>
                          )}
                          <div className="button-row">
                            <button
                              className="primary small"
                              disabled={busy}
                              onClick={() => resolvePending(true)}
                            >
                              Save reply & memories
                            </button>
                            <button
                              className="text-button"
                              disabled={busy}
                              onClick={() => resolvePending(false)}
                            >
                              Save reply only
                            </button>
                            <button
                              className="text-button"
                              onClick={() => inspect(draft.turn_id)}
                            >
                              Inspect
                            </button>
                          </div>
                        </div>
                      </>
                    ) : (
                      <div className="notice">
                        <p>
                          This turn is unfinished. No automatic retry will be
                          made.
                        </p>
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() => resolvePending(null)}
                        >
                          Discard unfinished turn
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
            <div ref={bottom} />
          </div>
          {!atBottom && (
            <button
              className="jump-bottom"
              aria-label="Jump to latest message"
              onClick={() => {
                setAtBottom(true);
                bottom.current?.scrollIntoView({
                  behavior: reduceMotion ? "instant" : "smooth",
                });
              }}
            >
              <ArrowDown size={17} />
            </button>
          )}
          <div className="composer-area">
            <ErrorNote message={error} />
            {audioUrl && (
              <div className="audio-player">
                <audio controls src={audioUrl} />
                <button
                  className="icon-button"
                  aria-label="Close audio player"
                  onClick={() => {
                    release(audioUrl);
                    setAudioUrl("");
                  }}
                >
                  <X size={16} />
                </button>
              </div>
            )}
            {!boot.capabilities.chat && (
              <button
                className="connect-notice"
                onClick={() => {
                  setProviderTab("chat");
                  setPanel("settings");
                }}
              >
                <span>
                  <span className="status-dot" />
                  Your studio is ready. Connect a model to start chatting.
                </span>
                <ArrowRight size={16} />
              </button>
            )}
            <form
              className={"composer " + (busy ? "is-busy" : "")}
              onSubmit={send}
            >
              {!!attachments.length && (
                <div className="attachment-tray">
                  {attachments.map((a, i) => (
                    <div className="attachment" key={a.url}>
                      <img src={a.url} alt={a.name} />
                      <span>{a.name}</span>
                      <button
                        type="button"
                        aria-label={"Remove " + a.name}
                        onClick={() => {
                          release(a.url);
                          setAttachments((items) =>
                            items.filter((_, index) => index !== i),
                          );
                        }}
                      >
                        <X size={13} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              {(recorder.active || transcribing) && (
                <div className="recording-strip">
                  <div className="recording-status" role="status">
                    {recorder.phase === "recording" ? (
                      <>
                        <span className="recording-dot" />
                        <span>
                          Recording{" "}
                          <time aria-live="off">
                            {Math.floor(recorder.seconds / 60)}:
                            {String(recorder.seconds % 60).padStart(2, "0")}
                          </time>
                        </span>
                      </>
                    ) : (
                      <Busy
                        text={
                          transcribing
                            ? "Transcribing your voice…"
                            : recorder.phase === "requesting"
                              ? "Allow microphone access…"
                              : "Finishing recording…"
                        }
                      />
                    )}
                  </div>
                  {recorder.active && (
                    <button
                      type="button"
                      className="text-button"
                      onClick={recorder.cancel}
                      aria-label="Cancel recording"
                    >
                      Cancel
                    </button>
                  )}
                </div>
              )}
              <textarea
                ref={composer}
                aria-label="Message"
                placeholder={
                  character
                    ? "Tell " + character.name + " what’s on your mind…"
                    : "Create a companion to begin…"
                }
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                disabled={!character || busy || !!conversation?.pending}
                maxLength={16000}
                rows={1}
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing
                  ) {
                    e.preventDefault();
                    void send();
                  }
                }}
              />
              <div className="composer-toolbar">
                <div className="composer-tools">
                  <button
                    type="button"
                    aria-label="Attach image"
                    title="Attach image for vision"
                    disabled={!character || busy || !!conversation?.pending}
                    onClick={() =>
                      capability("vision", () => imageInput.current?.click())
                    }
                  >
                    <ImagePlus size={18} />
                  </button>
                  <button
                    type="button"
                    className={
                      recorder.phase === "recording" ? "recording-button" : ""
                    }
                    aria-label={
                      recorder.phase === "recording"
                        ? "Stop recording and transcribe"
                        : "Record voice message"
                    }
                    title={
                      recorder.phase === "recording"
                        ? "Stop recording and transcribe"
                        : "Record a voice message"
                    }
                    aria-pressed={recorder.phase === "recording"}
                    disabled={
                      !character ||
                      loading ||
                      working ||
                      !!conversation?.pending ||
                      recorder.phase === "requesting" ||
                      recorder.phase === "stopping"
                    }
                    onClick={() => {
                      if (recorder.phase === "recording") recorder.stop();
                      else
                        capability("transcription", () => {
                          void recorder.start();
                        });
                    }}
                  >
                    {recorder.phase === "recording" ? (
                      <Square size={15} fill="currentColor" />
                    ) : (
                      <Mic size={18} />
                    )}
                  </button>
                </div>
                <span className="composer-hint">
                  {transcribing
                    ? "Transcribing…"
                    : busy
                      ? "One moment…"
                      : "A little space to be yourself."}
                </span>
                <button
                  type="submit"
                  className="send-button"
                  aria-label="Send message"
                  disabled={
                    !message.trim() ||
                    busy ||
                    !character ||
                    !!conversation?.pending
                  }
                >
                  <ArrowUp size={19} />
                </button>
              </div>
            </form>
            <input
              type="file"
              hidden
              accept="image/png,image/jpeg,image/webp"
              multiple
              ref={imageInput}
              onChange={(e) => attach(e.target.files)}
            />
            <div className="composer-footer">
              <span>
                <LockKeyhole size={10} />
                Local storage. Your chosen model.
              </span>
              <span>
                COMPANION ENGINE<span className="footer-dot">·</span>ANIMA LAB
              </span>
            </div>
          </div>
        </main>
      </div>
      <Sheet
        open={!!panel}
        onClose={() => setPanel("")}
        title={
          {
            "": "",
            settings: "Make it yours.",
            create: "Someone new.",
            edit: "A little personality.",
            memory: "The little things.",
            trace: "Behind this reply.",
            characters: "Your companions.",
          }[panel] || ""
        }
        description={
          {
            "": "",
            settings:
              "Connect your own providers. Each capability can use a different model.",
            create:
              "Choose a starting point, or write a character of your own.",
            edit: "Give your companion their own voice and boundaries.",
            memory:
              "Grounded details, saved locally. Review the source, correct a detail, or remove it.",
            trace:
              "A transparent view of the exchange and the context that shaped it.",
            characters: "Pick up a conversation, or start with someone new.",
          }[panel] || ""
        }
        wide={panel === "trace"}
      >
        {panel === "settings" && (
          <ProviderSettings
            token={token}
            boot={boot}
            initialTab={providerTab}
            onSave={async () => {
              await refreshBoot();
            }}
          />
        )}
        {(panel === "create" || panel === "edit") && (
          <CharacterForm
            key={panel + selected}
            token={token}
            initial={panel === "edit" ? character : undefined}
            reference={
              panel === "edit"
                ? boot.appearance_references?.[selected]
                : undefined
            }
            onSave={saveCharacter}
            onClose={() => setPanel("")}
          />
        )}
        {panel === "memory" && (
          <MemoryPanel
            token={token}
            characterId={selected}
            onSource={inspect}
            onChanged={() => {
              void api<MemoryPage>(token, "memory_panel", undefined, {
                character_id: selected,
              }).then((p) => setMemoryCount(p.total));
            }}
          />
        )}
        {panel === "trace" && (
          <TurnInspector
            key={traceId}
            token={token}
            characterId={selected}
            turnId={traceId}
          />
        )}
        {panel === "characters" && (
          <div className="mobile-character-list">
            {boot.characters.map((c) => (
              <button
                key={c.id}
                onClick={() => {
                  setSelected(c.id);
                  setPanel("");
                }}
              >
                <Avatar character={c} />
                <span>
                  {c.name}
                  <small>{c.persona.slice(0, 75)}</small>
                </span>
                {selected === c.id && <Check size={17} />}
              </button>
            ))}
            <button className="secondary" onClick={() => setPanel("create")}>
              <Plus size={17} />
              Create a character
            </button>
            {character && (
              <button className="text-button" onClick={() => setPanel("edit")}>
                Edit {character.name} <SlidersHorizontal size={14} />
              </button>
            )}
          </div>
        )}
      </Sheet>
      {callMode && character && (
        <CallOverlay
          token={token}
          character={character}
          mode={callMode}
          reference={boot.appearance_references?.[selected]}
          onClose={() => {
            setCallMode(null);
            void reload().catch((e) => setError(errorText(e)));
          }}
        />
      )}
      <AnimatePresence>
        {toast && (
          <motion.div
            className="toast"
            role="status"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
          >
            <Check size={15} />
            {toast}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
