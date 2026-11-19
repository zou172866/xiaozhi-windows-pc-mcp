import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AnimatePresence, motion } from "motion/react";
import {
  Mic,
  MicOff,
  PhoneOff,
  Camera,
  CameraOff,
  Hand,
  LoaderCircle,
  Phone,
  Video,
  ArrowRight,
} from "lucide-react";
import { LiveCall, type LiveEvent } from "./liveCall";
import { StoredImage } from "./images";
import { portrait, type Character, type ImageArtifact } from "./api";
import "./calls.css";

export function CallOverlay({
  token,
  character,
  mode,
  onClose,
  reference,
}: {
  reference?: ImageArtifact | null;
  token: string;
  character: Character;
  mode: "voice" | "video";
  onClose: () => void;
}) {
  const [phase, setPhase] = useState("ready"),
    [notice, setNotice] = useState(""),
    [error, setError] = useState("");
  const [muted, setMuted] = useState(false),
    [camera, setCamera] = useState(false),
    [level, setLevel] = useState(0);
  const [videoReady, setVideoReady] = useState(false),
    [caption, setCaption] = useState<{ speaker: string; text: string } | null>(
      null,
    );
  const [elapsed, setElapsed] = useState(0),
    [language, setLanguage] = useState("zh-CN");
  const client = useRef<LiveCall | null>(null),
    remote = useRef<HTMLDivElement>(null),
    preview = useRef<HTMLVideoElement>(null);
  const started = useRef(false),
    mounted = useRef(true),
    rtc = useRef<LiveEvent["data"] | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      void client.current?.close();
    };
  }, []);
  useEffect(() => {
    if (!started.current || phase === "ended") return;
    const interval = setInterval(() => setElapsed((v) => v + 1), 1000);
    return () => clearInterval(interval);
  }, [phase === "ended", phase === "ready"]);
  function handle(event: LiveEvent) {
    if (!mounted.current) return;
    if (event.type === "state") setPhase(event.data.phase);
    if (event.type === "partial")
      setCaption({ speaker: "You", text: event.data.text });
    if (event.type === "transcript")
      setCaption({
        speaker: event.data.speaker === "user" ? "You" : character.name,
        text: event.data.text,
      });
    if (event.type === "error") setError(event.data.message);
    if (event.type === "notice") setNotice(event.data.message);
    if (event.type === "closed") {
      setPhase("ended");
      setLevel(0);
      if (event.data.cleanup_required)
        setError(
          "The provider could not confirm cleanup. Check the saved call before starting another.",
        );
    }
    if (event.type === "avatar" && remote.current) {
      rtc.current = event.data;
      void client.current?.avatar(event.data as any, remote.current, () => {
        if (mounted.current) setVideoReady(true);
      });
    }
  }
  function start() {
    if (started.current) return;
    started.current = true;
    setPhase("microphone");
    const call = new LiveCall(mode, handle, (v) => {
      if (mounted.current) setLevel(v);
    });
    client.current = call;
    void call.start(token, character.id, language);
  }
  async function end() {
    setPhase("ended");
    setLevel(0);
    await client.current?.close();
  }
  async function toggleCamera() {
    if (!preview.current) return;
    const next = !camera;
    try {
      await client.current?.setCamera(next, preview.current);
      setCamera(next);
    } catch {
      setNotice("Camera permission was not granted. You can keep talking.");
      setCamera(false);
    }
  }
  const labels: Record<string, string> = {
    ready: mode === "voice" ? "A little closer." : "Face to face.",
    microphone: "Opening your microphone",
    connecting: "Connecting voice",
    starting_avatar: "Bringing your companion to life",
    listening:
      mode === "video" && !videoReady
        ? "Waiting for the first video frame"
        : "Listening",
    thinking: "Thinking",
    speaking: "Speaking",
    ended: "Call ended",
  };
  const isLive = started.current && phase !== "ended";
  return (
    <Dialog.Root
      open
      onOpenChange={(v) => {
        if (!v) {
          void client.current?.close();
          onClose();
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="call-scrim" />
        <Dialog.Content
          className={"live-call " + mode}
          aria-describedby="call-description"
        >
          <div
            className="call-backdrop"
            style={{
              backgroundImage: `url(${portrait(character.id) || undefined})`,
            }}
          />
          <div
            className={"call-video " + (videoReady ? "visible" : "")}
            ref={remote}
            onClick={() =>
              remote.current
                ?.querySelectorAll("video")
                .forEach((v) => void v.play())
            }
          />
          <div className="call-shade" />
          <header className="call-top">
            <span className="call-wordmark">
              ANIMA <span>/ {mode === "voice" ? "VOICE" : "LIVE"}</span>
            </span>
            <span className="call-clock">
              {isLive
                ? `${Math.floor(elapsed / 60)
                    .toString()
                    .padStart(
                      2,
                      "0",
                    )}:${(elapsed % 60).toString().padStart(2, "0")}`
                : "YOUR SPACE"}
            </span>
          </header>
          <div className={"call-center " + (videoReady ? "small" : "")}>
            {!videoReady && (
              <div className="call-portrait">
                {reference ? (
                  <StoredImage
                    token={token}
                    characterId={character.id}
                    image={reference}
                    alt=""
                  />
                ) : portrait(character.id) ? (
                  <img src={portrait(character.id) || undefined} alt="" />
                ) : (
                  <span className="call-initial">
                    {character.name.slice(0, 1)}
                  </span>
                )}
                <motion.div
                  className="call-orbit"
                  animate={{
                    scale: 1 + Math.min(level * 2, 0.18),
                    opacity: 0.3 + Math.min(level * 5, 0.6),
                  }}
                  transition={{ duration: 0.16 }}
                />
              </div>
            )}
            <Dialog.Title>{character.name}</Dialog.Title>
            <p className="call-phase" role="status">
              {isLive &&
                ["microphone", "connecting", "starting_avatar"].includes(
                  phase,
                ) && <LoaderCircle className="spin" size={15} />}{" "}
              {labels[phase] || phase}
            </p>
            <Dialog.Description
              id="call-description"
              className={phase === "ready" ? "call-description" : "sr-only"}
            >
              {phase === "ready"
                ? "Same companion. Same memories. A different way to be here."
                : "A live call with your selected companion."}
            </Dialog.Description>
            {phase === "ready" && (
              <div className="call-invitation">
                <label>
                  Conversation language
                  <select
                    value={language}
                    onChange={(e) => setLanguage(e.target.value)}
                  >
                    <option value="zh-CN">中文</option>
                    <option value="en-US">English</option>
                    <option value="ja-JP">日本語</option>
                  </select>
                </label>
                <button className="call-start" onClick={start}>
                  {mode === "voice" ? <Phone size={19} /> : <Video size={19} />}{" "}
                  Start {mode} call <ArrowRight size={17} />
                </button>
                <p>
                  Microphone on. Your camera stays off until you enable it.
                  <br />
                  Uses your configured providers. Up to 10 minutes.
                </p>
              </div>
            )}
          </div>
          <video
            className={"call-self " + (camera ? "visible" : "")}
            ref={preview}
            muted
            playsInline
            aria-label="Your camera preview"
          />
          <div className="call-bottom">
            <AnimatePresence mode="wait">
              {caption && phase !== "ready" && (
                <motion.div
                  className="call-caption"
                  key={caption.speaker + caption.text}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.16 }}
                >
                  <span>{caption.speaker}</span>
                  <p>{caption.text}</p>
                </motion.div>
              )}
            </AnimatePresence>
            {error && (
              <p role="alert" className="call-notice error">
                {error}
              </p>
            )}
            {notice && <p className="call-notice">{notice}</p>}
            {isLive && (
              <div className="call-controls">
                <button
                  aria-label={muted ? "Unmute microphone" : "Mute microphone"}
                  aria-pressed={muted}
                  onClick={() => {
                    client.current?.mute(!muted);
                    setMuted(!muted);
                  }}
                >
                  {muted ? <MicOff /> : <Mic />}
                  <span>{muted ? "Unmute" : "Mute"}</span>
                </button>
                {mode === "video" && (
                  <button
                    aria-label={camera ? "Turn camera off" : "Turn camera on"}
                    aria-pressed={camera}
                    onClick={toggleCamera}
                  >
                    {camera ? <Camera /> : <CameraOff />}
                    <span>Camera</span>
                  </button>
                )}
                <button
                  aria-label="Interrupt reply"
                  onClick={() => client.current?.interrupt()}
                >
                  <Hand />
                  <span>Interrupt</span>
                </button>
                <button
                  className="call-hangup"
                  aria-label="End call"
                  onClick={() => void end()}
                >
                  <PhoneOff />
                  <span>End call</span>
                </button>
              </div>
            )}
            {!isLive && (
              <button className="call-back" onClick={onClose}>
                {phase === "ended" ? "Back to conversation" : "Not now"}
              </button>
            )}
            {isLive && (
              <div className="call-meter" aria-hidden="true">
                {[0.4, 0.7, 1, 0.65, 0.9, 0.5, 0.3].map((v, i) => (
                  <span
                    key={i}
                    style={{ height: 4 + Math.min(level * 180, 20) * v }}
                  />
                ))}
              </div>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
