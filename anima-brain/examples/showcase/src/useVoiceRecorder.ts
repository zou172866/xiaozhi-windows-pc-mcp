import { useCallback, useEffect, useRef, useState } from "react";

const MAX_SECONDS = 120;
const MAX_BYTES = 20 * 1024 * 1024;
const FORMATS = [
  "audio/webm;codecs=opus",
  "audio/mp4",
  "audio/ogg;codecs=opus",
];
type Phase = "idle" | "requesting" | "recording" | "stopping";
type Session = {
  cancelled: boolean;
  finishing: boolean;
  chunks: Blob[];
  bytes: number;
  stream?: MediaStream;
  recorder?: MediaRecorder;
  timer?: ReturnType<typeof setInterval>;
};

function release(session: Session) {
  clearInterval(session.timer);
  session.stream?.getTracks().forEach((track) => track.stop());
}

/** One recording at a time. Only an explicit stop (or duration limit) uploads audio. */
export function useVoiceRecorder({
  scope,
  onAudio,
  onError,
  onNotice,
}: {
  scope: string;
  onAudio: (audio: File) => Promise<void>;
  onError: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const current = useRef<Session | null>(null);
  const mounted = useRef(false);
  const callbacks = useRef({ onAudio, onError, onNotice });
  callbacks.current = { onAudio, onError, onNotice };

  const cancel = useCallback(() => {
    const session = current.current;
    current.current = null;
    if (session) {
      session.cancelled = true;
      session.chunks = [];
      // Ignore already queued data/stop events; releasing tracks is unconditional.
      try {
        if (session.recorder?.state !== "inactive") session.recorder?.stop();
      } catch {
        // The device may already have stopped the recorder.
      } finally {
        release(session);
      }
    }
    if (mounted.current) {
      setPhase("idle");
      setSeconds(0);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    setPhase("idle");
    setSeconds(0);
    window.addEventListener("pagehide", cancel);
    return () => {
      mounted.current = false;
      cancel();
      window.removeEventListener("pagehide", cancel);
    };
  }, [scope, cancel]);

  const stop = useCallback(() => {
    const session = current.current;
    if (!session || session.cancelled || session.finishing) return;
    if (!session.recorder) {
      cancel();
      return;
    }
    session.finishing = true;
    setPhase("stopping");
    try {
      if (session.recorder.state !== "inactive") session.recorder.stop();
    } catch {
      cancel();
      callbacks.current.onError(
        "The recording could not be finished. Please record again.",
      );
    } finally {
      // stop() queues the final data and stop events; the microphone can close now.
      release(session);
    }
  }, [cancel]);

  const start = useCallback(async () => {
    if (current.current) return;
    callbacks.current.onError("");
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      callbacks.current.onError(
        "Microphone recording is unavailable. Use a supported browser on HTTPS or localhost.",
      );
      return;
    }
    const mime = FORMATS.find((format) =>
      MediaRecorder.isTypeSupported(format),
    );
    if (!mime) {
      callbacks.current.onError(
        "This browser cannot record a supported audio format. Try a current version of Chrome or Safari.",
      );
      return;
    }
    const session: Session = {
      cancelled: false,
      finishing: false,
      chunks: [],
      bytes: 0,
    };
    current.current = session;
    setPhase("requesting");
    setSeconds(0);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // Permission may resolve after cancel, navigation, or another recording starts.
      if (
        current.current !== session ||
        session.cancelled ||
        !mounted.current
      ) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      session.stream = stream;
      const recorder = new MediaRecorder(stream, { mimeType: mime });
      session.recorder = recorder;
      recorder.ondataavailable = (event) => {
        if (
          session.cancelled ||
          current.current !== session ||
          !event.data.size
        )
          return;
        session.bytes += event.data.size;
        if (session.bytes > MAX_BYTES) {
          cancel();
          callbacks.current.onError(
            "The recording exceeded 20 MB and was discarded. Please record a shorter message.",
          );
          return;
        }
        session.chunks.push(event.data);
      };
      recorder.onerror = () => {
        if (current.current !== session || session.cancelled) return;
        cancel();
        callbacks.current.onError(
          "The microphone recording failed. Please check the device and try again.",
        );
      };
      recorder.onstop = () => {
        release(session);
        if (
          session.cancelled ||
          current.current !== session ||
          !mounted.current
        )
          return;
        current.current = null;
        setPhase("idle");
        const type = (recorder.mimeType || mime).split(";")[0];
        const extension =
          type === "audio/mp4" ? "m4a" : type === "audio/ogg" ? "ogg" : "webm";
        const recording = new File(session.chunks, "recording." + extension, {
          type,
        });
        session.chunks = [];
        if (!recording.size) {
          callbacks.current.onError("No audio was recorded. Please try again.");
          return;
        }
        void callbacks.current.onAudio(recording).catch(() => {
          if (mounted.current)
            callbacks.current.onError(
              "The recording could not be transcribed. Please try again.",
            );
        });
      };
      stream.getAudioTracks().forEach((track) => {
        track.addEventListener(
          "ended",
          () => {
            if (
              current.current === session &&
              !session.finishing &&
              !session.cancelled
            ) {
              callbacks.current.onNotice(
                "Microphone disconnected. Transcribing the recorded part.",
              );
              stop();
            }
          },
          { once: true },
        );
      });
      recorder.start(250);
      setPhase("recording");
      const started = performance.now();
      session.timer = setInterval(() => {
        if (current.current !== session || session.finishing) return;
        const elapsed = Math.floor((performance.now() - started) / 1000);
        setSeconds(Math.min(elapsed, MAX_SECONDS));
        if (elapsed >= MAX_SECONDS) {
          callbacks.current.onNotice(
            "Two-minute limit reached. Transcribing your recording.",
          );
          stop();
        }
      }, 250);
    } catch (error) {
      if (current.current !== session || session.cancelled || !mounted.current)
        return;
      cancel();
      const name = error instanceof DOMException ? error.name : "";
      callbacks.current.onError(
        name === "NotAllowedError" || name === "SecurityError"
          ? "Microphone access was blocked. Allow microphone access in your browser settings, then try again."
          : name === "NotFoundError"
            ? "No microphone was found. Connect one and try again."
            : name === "NotReadableError"
              ? "The microphone is unavailable. Check whether another app is using it."
              : "Could not start recording. Check your microphone and browser permissions.",
      );
    }
  }, [cancel, stop]);

  return { phase, seconds, active: phase !== "idle", start, stop, cancel };
}
