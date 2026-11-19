import { useState } from "react";
import { api, errorText, type Bootstrap } from "./api";
export function CallSettings({
  token,
  boot,
  onSave,
}: {
  token: string;
  boot: Bootstrap;
  onSave: () => Promise<void>;
}) {
  const [asr, setAsr] = useState(""),
    [vidu, setVidu] = useState(""),
    [resource, setResource] = useState(
      String(boot.settings.call_asr_resource || "volc.bigasr.sauc.duration"),
    );
  const [persist, setPersist] = useState(false),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  const [clearASR, setClearASR] = useState(false),
    [clearVidu, setClearVidu] = useState(false);
  return (
    <form
      className="form-stack"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setMessage("");
        try {
          await api(token, "settings", {
            settings: {
              call_asr_resource: resource,
              ...(asr ? { call_asr_key: asr } : {}),
              ...(vidu ? { vidu_key: vidu } : {}),
            },
            clear_keys: [
              ...(clearASR ? ["call_asr_key"] : []),
              ...(clearVidu ? ["vidu_key"] : []),
            ],
            persist,
          });
          await onSave();
          setAsr("");
          setVidu("");
          setMessage("Call settings saved.");
        } catch (e) {
          setMessage(errorText(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="helper">
        Calls use the chat model and memory already configured here. In Voice,
        select Doubao and set its model and key. Each companion also needs a
        voice ID.
      </p>
      <div className="call-setting-block">
        <h3>Live speech recognition</h3>
        <label>
          Doubao ASR key {boot.settings.call_asr_key ? "· configured" : ""}
          <input
            type="password"
            autoComplete="new-password"
            value={asr}
            onChange={(e) => setAsr(e.target.value)}
            placeholder="Blank keeps the saved key, or falls back to the Voice key"
          />
        </label>
        <label>
          Granted ASR resource
          <input
            value={resource}
            onChange={(e) => setResource(e.target.value)}
          />
        </label>
        <p className="helper">
          Use the resource enabled for your account. Voice cloning and ASR can
          require different keys.
        </p>
        {!!boot.settings.call_asr_key && (
          <label className="check-label">
            <input
              type="checkbox"
              checked={clearASR}
              onChange={(e) => setClearASR(e.target.checked)}
            />
            Remove ASR key
          </label>
        )}
      </div>
      <div className="call-setting-block">
        <h3>Animated video</h3>
        <label>
          Vidu API key {boot.settings.vidu_key ? "· configured" : ""}
          <input
            type="password"
            autoComplete="new-password"
            value={vidu}
            onChange={(e) => setVidu(e.target.value)}
            placeholder="Leave blank to keep the saved key"
          />
        </label>
        <p className="helper">
          Vidu S2 Component with managed Volcengine RTC. Upload the companion’s
          appearance reference in Edit. Provider recording is disabled.
        </p>
        {!!boot.settings.vidu_key && (
          <label className="check-label">
            <input
              type="checkbox"
              checked={clearVidu}
              onChange={(e) => setClearVidu(e.target.checked)}
            />
            Remove Vidu key
          </label>
        )}
      </div>
      <label className="check-label">
        <input
          type="checkbox"
          checked={persist}
          onChange={(e) => setPersist(e.target.checked)}
        />
        Remember settings on this computer
      </label>
      <p className="helper">
        Keys stay on the Python server. An ephemeral video-room token is shared
        with the browser only during a call.
      </p>
      {message && <p role="status">{message}</p>}
      <button className="primary" disabled={busy}>
        {busy ? "Saving…" : "Save call settings"}
      </button>
    </form>
  );
}
