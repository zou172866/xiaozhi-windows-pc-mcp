/** Browser transport only. ASR, memory, reply generation and TTS stay in Python. */
export type LiveEvent = {
  type: string;
  data: Record<string, any>;
  sequence: number;
};
export type AvatarConnection = {
  app_id: string;
  channel_id: string;
  user_id: string;
  token: string;
};
export class LiveCall {
  private ws?: WebSocket;
  private mic?: MediaStream;
  private camera?: MediaStream;
  private capture?: AudioContext;
  private playback?: AudioContext;
  private worklet?: AudioWorkletNode;
  private nodes = new Set<AudioBufferSourceNode>();
  private nextAudio = 0;
  private connected = false;
  private closed = false;
  private muted = false;
  private activeSequence = 0;
  private discardThrough = 0;
  private talkingSince = 0;
  private voiceStart = 0;
  private heartbeat?: number;
  private frameTimer?: number;
  private rtcCleanup?: () => Promise<void>;
  private openingVideo = false;
  private cameraVersion = 0;
  constructor(
    private mode: "voice" | "video",
    private onEvent: (e: LiveEvent) => void,
    private onLevel: (v: number) => void,
  ) {}
  send(type: string, values = {}) {
    if (this.ws?.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify({ type, ...values }));
  }
  async start(token: string, character: string, language: string) {
    try {
      this.playback = new AudioContext({ sampleRate: 24000 });
      await this.playback.resume();
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      if (this.closed) {
        mic.getTracks().forEach((t) => t.stop());
        return;
      }
      this.mic = mic;
      this.capture = new AudioContext();
      await this.capture.audioWorklet.addModule("/assets/call-capture.js");
      if (this.closed) return;
      this.worklet = new AudioWorkletNode(this.capture, "call-capture");
      this.capture.createMediaStreamSource(mic).connect(this.worklet);
      // A connected silent output keeps processing alive on Safari, without local mic echo.
      const silent = this.capture.createGain();
      silent.gain.value = 0;
      this.worklet.connect(silent).connect(this.capture.destination);
      await this.capture.resume();
      const ws = new WebSocket(
        `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/call`,
      );
      this.ws = ws;
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        if (this.closed) {
          ws.close();
          return;
        }
        ws.send(
          JSON.stringify({
            token,
            character_id: character,
            options: { mode: this.mode, language, max_seconds: 600 },
          }),
        );
        this.heartbeat = window.setInterval(() => this.send("ping"), 20000);
      };
      ws.onmessage = (e) => {
        if (this.closed) return;
        if (e.data instanceof ArrayBuffer) {
          if (this.mode === "voice") this.play(e.data);
          return;
        }
        const event: LiveEvent = JSON.parse(e.data);
        if (event.type === "state" && event.data.phase === "listening")
          this.connected = true;
        if (event.type === "state" && event.data.phase === "speaking") {
          this.activeSequence = event.sequence;
          this.talkingSince = performance.now();
        }
        if (event.type === "audio_end") this.talkingSince = 0;
        if (event.type === "interrupted") {
          this.discardThrough = Math.max(this.discardThrough, event.sequence);
          this.stopAudio();
        }
        this.onEvent(event);
        if (event.type === "closed") void this.close(false);
      };
      ws.onerror = () =>
        this.onEvent({
          type: "error",
          data: { message: "Could not connect to the call server." },
          sequence: 0,
        });
      ws.onclose = () => {
        if (!this.closed) {
          this.onEvent({
            type: "closed",
            data: { reason: "connection_closed" },
            sequence: 0,
          });
          void this.close(false);
        }
      };
      this.worklet.port.onmessage = ({ data }) => {
        if (this.closed) return;
        this.onLevel(this.muted ? 0 : data.rms);
        if (this.connected && ws.readyState === WebSocket.OPEN) {
          if (ws.bufferedAmount > 64000) {
            this.fail("Connection is too slow for a live call.");
            return;
          }
          ws.send(data.pcm);
        }
        // A sustained onset, with a grace window, avoids interrupting on tiny noises.
        const now = performance.now();
        if (
          !this.muted &&
          this.talkingSince &&
          now - this.talkingSince > 350 &&
          data.rms > 0.065
        ) {
          this.voiceStart ||= now;
          if (now - this.voiceStart >= 350) this.interrupt();
        } else this.voiceStart = 0;
      };
    } catch (e) {
      this.fail(e instanceof Error ? e.message : "Microphone unavailable.");
    }
  }
  private fail(message: string) {
    this.onEvent({ type: "error", data: { message }, sequence: 0 });
    this.onEvent({
      type: "closed",
      data: { reason: "client_error" },
      sequence: 0,
    });
    void this.close();
  }
  private play(raw: ArrayBuffer) {
    if (!this.playback || raw.byteLength < 6 || raw.byteLength % 2) return;
    const seq = new DataView(raw).getUint32(0, true);
    if (seq <= this.discardThrough) return;
    const pcm = new Int16Array(raw, 4),
      buffer = this.playback.createBuffer(1, pcm.length, 24000);
    const channel = buffer.getChannelData(0);
    pcm.forEach((v, i) => (channel[i] = v / 32768));
    const source = this.playback.createBufferSource();
    source.buffer = buffer;
    source.connect(this.playback.destination);
    this.nextAudio = Math.max(this.nextAudio, this.playback.currentTime + 0.03);
    source.start(this.nextAudio);
    this.nextAudio += buffer.duration;
    this.nodes.add(source);
    source.onended = () => {
      source.disconnect();
      this.nodes.delete(source);
    };
  }
  private stopAudio() {
    this.nodes.forEach((n) => {
      try {
        n.stop();
      } catch {
        /* already ended */
      }
      n.disconnect();
    });
    this.nodes.clear();
    this.nextAudio = 0;
    this.voiceStart = 0;
    this.talkingSince = 0;
  }
  interrupt() {
    this.discardThrough = Math.max(this.discardThrough, this.activeSequence);
    this.stopAudio();
    this.send("interrupt");
  }
  mute(enabled: boolean) {
    this.muted = enabled;
    this.mic?.getAudioTracks().forEach((t) => (t.enabled = !enabled));
    this.send("mute", { enabled });
  }
  async avatar(
    data: AvatarConnection,
    target: HTMLDivElement,
    onReady: () => void,
  ) {
    if (this.openingVideo || this.closed) return;
    this.openingVideo = true;
    try {
      const {
        default: RTC,
        MediaType,
        StreamIndex,
      } = await import("@volcengine/rtc");
      if (this.closed) return;
      const engine = RTC.createEngine(data.app_id);
      this.rtcCleanup = async () => {
        await engine.leaveRoom();
        RTC.destroyEngine(engine);
      };
      engine.on(RTC.events.onUserPublishStream, async (e) => {
        if (this.closed) return;
        await engine.subscribeStream(e.userId, MediaType.AUDIO_AND_VIDEO);
        engine.setRemoteVideoPlayer(StreamIndex.STREAM_INDEX_MAIN, {
          userId: e.userId,
          renderDom: target,
        });
      });
      engine.on(RTC.events.onAutoplayFailed, () =>
        this.onEvent({
          type: "notice",
          data: { message: "Tap the video to enable its sound." },
          sequence: 0,
        }),
      );
      // A decoded first frame is the readiness boundary. Stop a stalled stream
      // explicitly instead of leaving a frozen portrait that looks connected.
      let ready = false,
        lastTime = -1,
        advancedAt = performance.now();
      const watch = window.setInterval(() => {
        const video = target.querySelector("video");
        if (video && video.readyState >= 2 && video.videoWidth > 0) {
          if (!ready) {
            ready = true;
            onReady();
          }
          if (video.currentTime !== lastTime) {
            lastTime = video.currentTime;
            advancedAt = performance.now();
          } else if (performance.now() - advancedAt > 12000) {
            this.fail("The video stream stopped updating. The call has ended.");
          }
        }
      }, 200);
      const timeout = window.setTimeout(() => {
        if (!ready)
          this.fail("The avatar did not deliver video. The call has ended.");
      }, 45000);
      const previous = this.rtcCleanup;
      this.rtcCleanup = async () => {
        clearInterval(watch);
        clearTimeout(timeout);
        await previous();
      };
      await engine.joinRoom(
        data.token,
        data.channel_id,
        { userId: data.user_id },
        {
          isAutoPublish: false,
          isAutoSubscribeAudio: true,
          isAutoSubscribeVideo: true,
        },
      );
    } catch {
      if (!this.closed) this.fail("Could not join the avatar video stream.");
    }
  }
  async setCamera(enabled: boolean, preview: HTMLVideoElement) {
    const version = ++this.cameraVersion;
    clearInterval(this.frameTimer);
    this.camera?.getTracks().forEach((t) => t.stop());
    this.camera = undefined;
    this.send("frame", { data: null });
    preview.srcObject = null;
    if (!enabled || this.closed) return;
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" },
      audio: false,
    });
    if (this.closed || version !== this.cameraVersion) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    this.camera = stream;
    preview.srcObject = stream;
    await preview.play();
    const canvas = document.createElement("canvas");
    this.frameTimer = window.setInterval(() => {
      if (!this.connected || !preview.videoWidth || this.closed) return;
      canvas.width = Math.min(640, preview.videoWidth);
      canvas.height = Math.round(
        (preview.videoHeight * canvas.width) / preview.videoWidth,
      );
      canvas
        .getContext("2d")!
        .drawImage(preview, 0, 0, canvas.width, canvas.height);
      this.send("frame", {
        data: canvas.toDataURL("image/jpeg", 0.65).split(",")[1],
      });
    }, 2000);
  }
  async close(send = true) {
    if (this.closed) return;
    this.closed = true;
    this.connected = false;
    clearInterval(this.heartbeat);
    clearInterval(this.frameTimer);
    this.cameraVersion++;
    if (send) this.send("hangup");
    this.mic?.getTracks().forEach((t) => t.stop());
    this.camera?.getTracks().forEach((t) => t.stop());
    this.worklet?.disconnect();
    this.stopAudio();
    await Promise.allSettled([
      this.capture?.close(),
      this.playback?.close(),
      this.rtcCleanup?.(),
    ]);
    // Server gets both hangup and disconnect and cleans up the paid avatar session.
    this.ws?.close();
  }
}
