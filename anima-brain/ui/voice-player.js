import { applyHostTheme } from './host-theme.js';
import { App } from "@modelcontextprotocol/ext-apps";

const app = new App({ name: "Companion voice player", version: "0.2.1" }, {});
const $ = (id) => document.getElementById(id);
const audio = $("audio"), button = $("play");
let voice = null, generation = 0, url = null, phase = "connecting", progress = 0;
let zh = document.documentElement.lang.startsWith("zh"), message = "", failed = false;
const words = {
  en: {voice: "Voice", play: "Play voice message", pause: "Pause voice message", loading: "Loading voice message", save: "Save audio", unavailable: "This recording is unavailable.", retry: "Couldn't load. Tap to retry.", blocked: "Tap again to play.", media: "Can't play this recording. Tap to reload, or save the audio.", connection: "Player disconnected. Reopen this message.", ended: "Voice message finished"},
  zh: {voice: "语音", play: "播放语音", pause: "暂停语音", loading: "正在加载语音", save: "保存语音", unavailable: "这条语音暂不可用。", retry: "加载失败，点击重试。", blocked: "再点一下即可播放。", media: "暂时无法播放，点击重新加载，或保存语音。", connection: "播放器未连接，请重新打开这条消息。", ended: "语音播放完毕"},
};
const t = (key) => words[zh ? "zh" : "en"][key];
const duration = () => Number.isFinite(audio.duration) && audio.duration > 0
  ? `${Math.max(1, Math.round(phase === "playing" ? Math.max(0, audio.duration - audio.currentTime) : audio.duration))}″` : t("voice");
function render() {
  button.dataset.state = phase;
  button.disabled = !voice || phase === "loading";
  button.setAttribute("aria-busy", String(phase === "loading"));
  button.setAttribute("aria-pressed", String(phase === "playing"));
  button.setAttribute("aria-label", `${voice?.character_name || "Companion"} · ${t(phase === "playing" ? "pause" : phase === "loading" ? "loading" : "play")}`);
  $("duration").textContent = phase === "loading" ? `${progress}%` : duration();
  $("status").textContent = message ? t(message) : "";
  $("status").dataset.error = String(failed);
  $("download").setAttribute("aria-label", t("save"));
  $("download").title = t("save");
}
function context(ctx) {
  applyHostTheme(ctx);
  if (ctx?.theme === "light" || ctx?.theme === "dark") document.documentElement.dataset.theme = ctx.theme;
  if (ctx?.locale) {document.documentElement.lang = ctx.locale; zh = ctx.locale.startsWith("zh");}
  render();
}
app.onhostcontextchanged = context;
function release() {
  audio.pause(); audio.removeAttribute("src"); audio.load();
  $("download").hidden = true; $("download").removeAttribute("href");
  if (url) URL.revokeObjectURL(url);
  url = null;
}
app.ontoolresult = (result) => {
  generation++; voice = null; release(); message = ""; failed = false; progress = 0;
  $("announcement").textContent = "";
  const v = result.structuredContent;
  if (result.isError || !v || v.mime !== "audio/mpeg" || !Number.isSafeInteger(v.bytes)
      || v.bytes <= 0 || v.bytes > 25 * 1024 * 1024 || typeof v.artifact !== "string"
      || typeof v.character_id !== "string") {
    phase = "error"; message = "unavailable"; failed = true; render(); return;
  }
  voice = v; phase = "ready";
  const name = String(v.character_name || "Companion");
  $("name").textContent = name;
  $("avatar").textContent = Array.from(name.trim())[0] || "·";
  render();
};
async function play(current) {
  try { await audio.play(); }
  catch (error) {
    if (current !== generation) return;
    phase = "ready";
    message = error.name === "NotAllowedError" ? "blocked" : "media";
    failed = message === "media";
    render();
  }
}
button.onclick = async () => {
  if (!voice || phase === "loading") return;
  if (!audio.paused) {audio.pause(); return;}
  const current = generation, target = voice;
  message = ""; failed = false;
  if (url && !audio.error) {await play(current); return;}
  release(); phase = "loading"; progress = 0; render();
  try {
    const chunks = []; let offset = 0;
    while (offset < target.bytes) {
      const result = await app.callServerTool({name: "read_voice_chunk", arguments: {
        character_id: target.character_id, artifact: target.artifact, offset,
      }});
      if (current !== generation) return;
      const data = result.structuredContent, encoded = result._meta?.audio_base64;
      if (result.isError || !data || typeof encoded !== "string" || encoded.length > 350000
          || data.offset !== offset || data.total !== target.bytes) throw new Error("Invalid chunk");
      const part = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
      if (!part.length || part.length > 262144 || data.next_offset !== offset + part.length
          || data.next_offset > target.bytes || data.eof !== (data.next_offset === target.bytes))
        throw new Error("Invalid audio offset");
      chunks.push(part); offset = data.next_offset;
      progress = Math.round(offset / target.bytes * 100); render();
    }
    url = URL.createObjectURL(new Blob(chunks, {type: "audio/mpeg"}));
    audio.src = url;
    $("download").href = url; $("download").download = "companion-voice.mp3";
    $("download").hidden = false; phase = "ready"; render();
    await play(current);
  } catch {
    if (current !== generation) return;
    phase = "error"; message = "retry"; failed = true; render();
  }
};
audio.onloadedmetadata = () => {if (voice) render();};
audio.ontimeupdate = () => {if (voice) $("duration").textContent = duration();};
audio.onplaying = () => {if (voice) {phase = "playing"; message = ""; failed = false; render();}};
audio.onpause = () => {if (voice && phase === "playing") {phase = "ready"; render();}};
audio.onended = () => {if (voice) {phase = "ready"; render(); $("announcement").textContent = t("ended");}};
audio.onerror = () => {if (voice && url) {phase = "error"; message = "media"; failed = true; render();}};
window.addEventListener("pagehide", () => {generation++; voice = null; release();});
try { await app.connect(); context(app.getHostContext()); }
catch {voice = null; phase = "error"; message = "connection"; failed = true; render();}
