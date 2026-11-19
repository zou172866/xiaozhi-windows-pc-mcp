import { useRef, useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  X,
  ArrowUpRight,
  Check,
  LoaderCircle,
  Plus,
  ImagePlus,
} from "lucide-react";
import {
  api,
  errorText,
  fileData,
  portrait,
  type Bootstrap,
  type Character,
  type Settings,
  type ImageArtifact,
} from "./api";
import { CallSettings } from "./CallSettings";
import { StoredImage } from "./images";

export function Sheet({
  open,
  onClose,
  title,
  description,
  children,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description: string;
  children: ReactNode;
  wide?: boolean;
}) {
  const opener = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(value) => {
        if (!value) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="sheet-overlay" />
        <Dialog.Content
          className={"sheet" + (wide ? " sheet-wide" : "")}
          onOpenAutoFocus={() => {
            opener.current = document.activeElement as HTMLElement;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (opener.current?.isConnected) opener.current.focus();
          }}
        >
          <div className="sheet-heading">
            <div>
              <span className="eyebrow">COMPANION STUDIO</span>
              <Dialog.Title>{title}</Dialog.Title>
            </div>
            <Dialog.Close className="icon-button" aria-label="Close panel">
              <X size={20} />
            </Dialog.Close>
          </div>
          <Dialog.Description className="sheet-description">
            {description}
          </Dialog.Description>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function Busy({ text = "Working…" }: { text?: string }) {
  return (
    <span className="busy">
      <LoaderCircle size={16} className="spin" />
      {text}
    </span>
  );
}
export function ErrorNote({ message }: { message: string }) {
  return message ? (
    <p className="error-note" role="alert">
      {message}
    </p>
  ) : null;
}
export function Avatar({
  character,
  small = false,
}: {
  character: Character;
  small?: boolean;
}) {
  const src = portrait(character.id);
  return (
    <span className={"avatar" + (small ? " avatar-small" : "")}>
      {src ? <img src={src} alt="" /> : character.name.slice(0, 1)}
    </span>
  );
}
export function CharacterForm({
  token,
  initial,
  reference,
  onSave,
  onClose,
}: {
  token: string;
  initial?: Character;
  reference?: ImageArtifact | null;
  onSave: (c: Character) => void | Promise<void>;
  onClose: () => void;
}) {
  const [preset, setPreset] = useState("custom");
  const [name, setName] = useState(initial?.name || "");
  const [persona, setPersona] = useState(initial?.persona || "");
  const [policy, setPolicy] = useState<Character["memory_policy"]>(
    initial?.memory_policy || "automatic",
  );
  const [voice, setVoice] = useState(initial?.voice_id || "");
  const [appearance, setAppearance] = useState(initial?.appearance || "");
  const [referenceData, setReferenceData] = useState<
    string | null | undefined
  >();
  const [referencePreview, setReferencePreview] = useState("");
  const [readingImage, setReadingImage] = useState(false);
  const referenceInput = useRef<HTMLInputElement>(null);
  const newId = useRef(crypto.randomUUID());
  const [romance, setRomance] = useState(initial?.romance_enabled || false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const presets = [
    {
      id: "rina",
      name: "Rina",
      persona:
        "You are Rina, a fictional companion. Warm, curious and a little playful, you notice small details and enjoy music and quiet city walks. Speak naturally and briefly. Respect the user’s boundaries, be honest about uncertainty, and never invent shared experiences.",
    },
    {
      id: "sean",
      name: "Sean",
      persona:
        "You are Sean, a fictional companion. Thoughtful, quietly funny and grounded, you enjoy photography, coffee and slow mornings. Listen before offering advice. Keep replies conversational and respect the user’s independence. Never invent shared experiences.",
    },
  ];
  async function chooseReference(file?: File) {
    if (!file) return;
    setError("");
    setReadingImage(true);
    try {
      if (!["image/png", "image/jpeg", "image/webp"].includes(file.type))
        throw new Error("Choose a PNG, JPEG or WebP image.");
      if (!file.size || file.size > 10 * 1024 * 1024)
        throw new Error("Keep the reference image below 10 MB.");
      const data = await fileData(file);
      setReferenceData(data);
      setReferencePreview("data:" + file.type + ";base64," + data);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setReadingImage(false);
      if (referenceInput.current) referenceInput.current.value = "";
    }
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const c = await api<Character>(
        token,
        initial ? "update_character" : "create_character",
        {
          spec: {
            ...(initial || {
              id:
                (preset === "custom" ? "companion" : preset) +
                "-" +
                newId.current,
              relationship_enabled: true,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
              core_persona: "",
              appearance: "",
            }),
            name: name.trim(),
            persona: persona.trim(),
            memory_policy: policy,
            voice_id: voice,
            appearance: appearance.trim(),
            romance_enabled: romance,
          },
          ...(referenceData !== undefined
            ? { reference_image: referenceData }
            : {}),
        },
      );
      await onSave(c);
      onClose();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} className="form-stack">
      {!initial && (
        <>
          <div className="preset-grid">
            {presets.map((p) => (
              <button
                type="button"
                className={"preset " + (preset === p.id ? "selected" : "")}
                key={p.id}
                onClick={() => {
                  setPreset(p.id);
                  setName(p.name);
                  setPersona(p.persona);
                }}
              >
                <img
                  src={"/portraits/" + p.id + ".jpg"}
                  alt={p.name + " sample character"}
                />
                <span>
                  {p.name}
                  <ArrowUpRight size={16} />
                </span>
              </button>
            ))}
            <button
              type="button"
              className={
                "preset custom " + (preset === "custom" ? "selected" : "")
              }
              onClick={() => {
                setPreset("custom");
                setName("");
                setPersona("");
              }}
            >
              <Plus size={24} />
              <span>Your own</span>
            </button>
          </div>
          <p className="helper">
            Rina and Sean are starter personas for this SDK example. Their
            behavior differs from the Anima app.
          </p>
        </>
      )}
      <label>
        Name
        <input
          required
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="What should we call them?"
        />
      </label>
      <label>
        Personality
        <textarea
          required
          maxLength={16000}
          rows={5}
          value={persona}
          onChange={(e) => setPersona(e.target.value)}
          placeholder="How they speak, what they care about, and the boundaries they respect."
        />
      </label>
      <label>
        Memory saving
        <select
          value={policy}
          onChange={(e) =>
            setPolicy(e.target.value as Character["memory_policy"])
          }
        >
          <option value="automatic">Automatic · save grounded memories</option>
          <option value="review">Review · approve requested memories</option>
          <option value="off">Off · do not save new memories</option>
        </select>
      </label>
      <p className="helper">
        This controls new memory writes. Existing memories and conversation
        history can still be used.
      </p>
      <section className="appearance-editor" aria-label="Character appearance">
        <div className="appearance-heading">
          <span className="eyebrow">APPEARANCE</span>
          <span>For their pictures</span>
        </div>
        <p className="helper">
          Add a reference to keep their look consistent when you ask for a
          selfie. Saved with this character and sent to your image provider when
          generating pictures.
        </p>
        {(referencePreview || (reference && referenceData !== null)) && (
          <figure className="reference-preview">
            {referencePreview ? (
              <img
                src={referencePreview}
                alt="Selected character appearance reference"
              />
            ) : (
              initial &&
              reference && (
                <StoredImage
                  token={token}
                  characterId={initial.id}
                  image={reference}
                  alt="Saved character appearance reference"
                />
              )
            )}
            <button
              type="button"
              className="icon-button"
              aria-label="Remove appearance reference"
              disabled={busy || readingImage}
              onClick={() => {
                setReferenceData(null);
                setReferencePreview("");
              }}
            >
              <X size={16} />
            </button>
          </figure>
        )}
        <input
          type="file"
          hidden
          ref={referenceInput}
          accept="image/png,image/jpeg,image/webp"
          onChange={(e) => void chooseReference(e.target.files?.[0])}
        />
        <button
          type="button"
          className="secondary reference-upload"
          disabled={busy || readingImage}
          onClick={() => referenceInput.current?.click()}
        >
          <ImagePlus size={17} />
          {readingImage
            ? "Reading image…"
            : referencePreview || (reference && referenceData !== null)
              ? "Replace reference image"
              : "Upload reference image"}
        </button>
        <span className="helper">PNG, JPEG or WebP · up to 10 MB</span>
        <label>
          Appearance notes
          <textarea
            value={appearance}
            onChange={(e) => setAppearance(e.target.value)}
            maxLength={2000}
            rows={3}
            placeholder="Hair, clothing, visual style, or details to keep across pictures."
          />
        </label>
      </section>
      <details className="advanced">
        <summary>Voice & relationship preferences</summary>
        <label>
          Voice ID
          <input
            value={voice}
            onChange={(e) => setVoice(e.target.value)}
            placeholder="Voice from your configured speech provider"
            maxLength={200}
          />
        </label>
        <label className="check-label">
          <input
            type="checkbox"
            checked={romance}
            onChange={(e) => setRomance(e.target.checked)}
          />
          Allow romantic character expression
        </label>
      </details>
      <ErrorNote message={error} />
      <button disabled={busy || readingImage} className="primary" type="submit">
        {busy ? (
          <Busy text="Saving…" />
        ) : initial ? (
          "Save character"
        ) : (
          "Meet your companion"
        )}
      </button>
    </form>
  );
}
const groups: Record<string, { label: string; prefix: string; hint: string }> =
  {
    chat: {
      label: "Conversation",
      prefix: "",
      hint: "A Chat Completions-compatible endpoint. The model must support JSON responses.",
    },
    vision: {
      label: "Vision",
      prefix: "vision_",
      hint: "Used when you attach an image. Configure a model that accepts image inputs.",
    },
    transcription: {
      label: "Transcription",
      prefix: "transcription_",
      hint: "A compatible audio/transcriptions endpoint. Transcripts are editable before sending.",
    },
    speech: {
      label: "Voice",
      prefix: "speech_",
      hint: "Choose a speech provider, then set a voice ID on your character.",
    },
  };
export function ProviderSettings({
  token,
  boot,
  onSave,
  initialTab = "chat",
}: {
  token: string;
  initialTab?: string;
  boot: Bootstrap;
  onSave: () => Promise<void>;
}) {
  const [tab, setTab] = useState(initialTab);
  return (
    <>
      <div className="segmented" aria-label="Provider capability">
        <button
          type="button"
          aria-pressed={tab === "calls"}
          onClick={() => setTab("calls")}
        >
          Calls
        </button>
        {Object.entries(groups).map(([id, g]) => (
          <button
            type="button"
            key={id}
            aria-pressed={tab === id}
            onClick={() => setTab(id)}
          >
            {g.label}
          </button>
        ))}
      </div>
      {boot.preview_mode ? (
        <div className="notice">
          This is an offline preview with scripted replies. Start without{" "}
          <code>--demo</code> to connect your own model.
        </div>
      ) : tab === "calls" ? (
        <CallSettings token={token} boot={boot} onSave={onSave} />
      ) : (
        <ProviderFields
          key={tab}
          token={token}
          tab={tab}
          boot={boot}
          onSave={onSave}
        />
      )}
      <div className="settings-footnote">
        <span className="eyebrow">YOUR MODEL, YOUR STORAGE</span>
        <p>
          Chat history and memories stay on this computer. Messages and relevant
          context are sent to the providers you configure when you use them.
        </p>
        <a href="https://anima-lab.io" target="_blank" rel="noreferrer">
          Looking for the ready-to-use Anima app? <ArrowUpRight size={14} />
        </a>
      </div>
    </>
  );
}
function ProviderFields({
  token,
  tab,
  boot,
  onSave,
}: {
  token: string;
  tab: string;
  boot: Bootstrap;
  onSave: () => Promise<void>;
}) {
  const group = groups[tab],
    prefix = group.prefix;
  const keyName = prefix ? prefix + "key" : "api_key";
  const [values, setValues] = useState<Settings>({
    ...boot.settings,
    [keyName]: "",
  });
  const [persist, setPersist] = useState(false);
  const [clear, setClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const field = (
    key: string,
    label: string,
    placeholder: string,
    type = "text",
  ) => (
    <label key={key}>
      {label}
      <input
        type={type}
        value={String(values[key] ?? "")}
        onChange={(e) => {
          setSaved(false);
          setValues({ ...values, [key]: e.target.value });
        }}
        placeholder={placeholder}
        autoComplete={type === "password" ? "new-password" : "off"}
      />
    </label>
  );
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSaved(false);
    const keys =
      tab === "speech"
        ? ["speech_provider", "speech_model", keyName]
        : [prefix + "base_url", prefix + "model", keyName];
    const updates = Object.fromEntries(
      keys.filter((k) => k !== keyName || values[k]).map((k) => [k, values[k]]),
    );
    try {
      await api(token, "settings", {
        settings: updates,
        clear_keys: clear ? [keyName] : [],
        persist,
      });
      await onSave();
      setValues((v) => ({ ...v, [keyName]: "" }));
      setClear(false);
      setSaved(true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} className="form-stack">
      <p className="helper">{group.hint}</p>
      {tab === "speech" ? (
        <label>
          Provider
          <select
            value={String(values.speech_provider)}
            onChange={(e) =>
              setValues({ ...values, speech_provider: e.target.value })
            }
          >
            <option value="elevenlabs">ElevenLabs</option>
            <option value="minimax">MiniMax</option>
            <option value="doubao">Doubao</option>
          </select>
        </label>
      ) : (
        field(
          prefix + "base_url",
          "Base URL",
          "https://your-provider.example/v1",
          "url",
        )
      )}
      {field(prefix + "model", "Model", "Your provider’s model ID")}
      {field(
        keyName,
        boot.settings[keyName] ? "API key · already configured" : "API key",
        boot.settings[keyName]
          ? "Leave blank to keep the existing key"
          : "Stored on the Python server, never in browser storage",
        "password",
      )}
      {!!boot.settings[keyName] && (
        <label className="check-label">
          <input
            type="checkbox"
            checked={clear}
            onChange={(e) => setClear(e.target.checked)}
          />
          Remove the existing key
        </label>
      )}
      <label className="check-label">
        <input
          type="checkbox"
          checked={persist}
          onChange={(e) => setPersist(e.target.checked)}
        />
        Remember provider settings on this computer
      </label>
      <p className="helper">
        {persist
          ? `Keys will be saved to ${boot.credential_storage.save_destination === "keychain" ? "macOS Keychain" : "a private local file"}.`
          : "Without saving, changes last until this server stops."}{" "}
        Saving configuration does not call the provider.
      </p>
      <ErrorNote message={error} />
      <button className="primary" disabled={busy}>
        {busy ? (
          <Busy text="Saving…" />
        ) : saved ? (
          <>
            <Check size={16} />
            Configuration saved
          </>
        ) : (
          "Save configuration"
        )}
      </button>
    </form>
  );
}
