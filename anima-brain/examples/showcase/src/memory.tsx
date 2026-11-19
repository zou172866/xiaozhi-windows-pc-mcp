import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  Check,
  Download,
  Pencil,
  Search,
  Trash2,
} from "lucide-react";
import {
  api,
  downloadJson,
  errorText,
  type Inspection,
  type Memory,
  type MemoryPage,
} from "./api";
import { Busy, ErrorNote } from "./components";

export function MemoryPanel({
  token,
  characterId,
  onSource,
  onChanged,
}: {
  token: string;
  characterId: string;
  onSource: (id: string) => void;
  onChanged: () => void;
}) {
  const [items, setItems] = useState<Memory[]>([]),
    [total, setTotal] = useState(0),
    [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [editing, setEditing] = useState(""),
    [deleting, setDeleting] = useState("");
  const [text, setText] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    api<MemoryPage>(token, "memory_panel", undefined, {
      character_id: characterId,
    })
      .then((p) => {
        if (active) {
          setItems(p.facts);
          setTotal(p.total);
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
  }, [token, characterId]);
  async function more() {
    setLoading(true);
    try {
      const p = await api<MemoryPage>(token, "memory_panel", undefined, {
        character_id: characterId,
        offset: String(items.length),
      });
      setItems([...items, ...p.facts]);
      setTotal(p.total);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }
  async function change(memory: Memory, remove = false) {
    setBusy(true);
    setError("");
    try {
      if (remove) {
        await api(token, "delete_memory", {
          character_id: characterId,
          memory_id: memory.id,
          expected_revision: memory.revision,
        });
        setItems(items.filter((m) => m.id !== memory.id));
        setTotal(total - 1);
        setDeleting("");
      } else {
        const m = await api<Memory>(token, "correct_memory", {
          character_id: characterId,
          memory_id: memory.id,
          expected_revision: memory.revision,
          draft: {
            key: memory.key,
            text: text.trim(),
            category: memory.category,
            source_quote: text.trim(),
            details: memory.details,
          },
        });
        setItems(items.map((x) => (x.id === m.id ? m : x)));
        setEditing("");
      }
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const filtered = items.filter((m) =>
    (m.text + " " + m.category).toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <>
      <div className="memory-intro">
        <span className="memory-number">
          {total.toString().padStart(2, "0")}
        </span>
        <div>
          little things, remembered.<p>You can always change your mind.</p>
        </div>
      </div>
      <label className="search-box">
        <Search size={17} />
        <input
          aria-label="Filter loaded memories"
          placeholder="Find a detail…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      {items.length < total && (
        <p className="helper">
          Showing {items.length} of {total}. Search filters the loaded memories.
        </p>
      )}
      <ErrorNote message={error} />
      <div className="memory-list">
        {filtered.map((m) => (
          <article className="memory-card" key={m.id}>
            <div className="memory-meta">
              <span className="eyebrow">{m.category}</span>
              <span>
                {new Date(m.updated_at * 1000).toLocaleDateString(undefined, {
                  month: "short",
                  day: "numeric",
                })}
              </span>
            </div>
            {editing === m.id ? (
              <div className="form-stack">
                <label>
                  Correct this memory
                  <textarea
                    autoFocus
                    value={text}
                    maxLength={2000}
                    rows={3}
                    onChange={(e) => setText(e.target.value)}
                  />
                </label>
                <p className="helper">
                  Saved as your manual correction, with a new revision. The
                  original conversation is unchanged.
                </p>
                <div className="button-row">
                  <button
                    className="primary small"
                    disabled={busy || !text.trim()}
                    onClick={() => change(m)}
                  >
                    <Check size={15} />
                    Save correction
                  </button>
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => setEditing("")}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <p className="memory-text">{m.text}</p>
            )}
            <details className="source-detail">
              <summary>
                {m.origin === "manual" ? "Manual note" : "Source & provenance"}
              </summary>
              <blockquote>“{m.source_quote}”</blockquote>
              <p className="helper">
                Revision {m.revision} · {m.origin.replaceAll("_", " ")}
              </p>
              {m.source_turn_id && (
                <button
                  className="text-button"
                  onClick={() => onSource(m.source_turn_id!)}
                >
                  View original exchange <ArrowUpRight size={14} />
                </button>
              )}
            </details>
            {deleting === m.id ? (
              <div className="delete-confirm">
                <p>Remove this memory? The source conversation will remain.</p>
                <div className="button-row">
                  <button
                    disabled={busy}
                    className="danger-button"
                    onClick={() => change(m, true)}
                  >
                    Remove memory
                  </button>
                  <button
                    disabled={busy}
                    className="text-button"
                    onClick={() => setDeleting("")}
                  >
                    Keep it
                  </button>
                </div>
              </div>
            ) : (
              <div className="memory-actions">
                <button
                  className="text-button"
                  onClick={() => {
                    setEditing(m.id);
                    setText(m.text);
                  }}
                >
                  <Pencil size={13} />
                  Edit
                </button>
                <button
                  className="text-button"
                  onClick={() => setDeleting(m.id)}
                >
                  <Trash2 size={13} />
                  Remove
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
      {loading ? (
        <Busy text="Reading memories…" />
      ) : !filtered.length ? (
        <div className="quiet-empty">
          <span>Nothing here yet.</span>
          <p>
            {query
              ? "Try a different word."
              : "As you talk, grounded details can become memories. Try “Remember that I prefer quiet evenings.”"}
          </p>
        </div>
      ) : null}
      {items.length < total && !loading && (
        <button className="secondary full" onClick={more}>
          Load more memories
        </button>
      )}
    </>
  );
}
export function TurnInspector({
  token,
  characterId,
  turnId,
}: {
  token: string;
  characterId: string;
  turnId: string;
}) {
  const [data, setData] = useState<Inspection | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api<Inspection>(token, "turn", undefined, {
      character_id: characterId,
      turn_id: turnId,
    })
      .then((x) => {
        if (active) setData(x);
      })
      .catch((e) => {
        if (active) setError(errorText(e));
      });
    return () => {
      active = false;
    };
  }, [token, characterId, turnId]);
  return (
    <>
      <ErrorNote message={error} />
      {!data && !error ? (
        <Busy text="Reading saved context…" />
      ) : (
        data && (
          <>
            <div className="inspected-exchange">
              <span className="eyebrow">YOU</span>
              <p>{data.user_message}</p>
              <span className="eyebrow">COMPANION</span>
              <p>{data.reply || "No reply was saved."}</p>
            </div>
            {data.generation && (
              <div className="trace-stats">
                <div>
                  <b>{String(data.generation.model)}</b>
                  <span>Reply model</span>
                </div>
                <div>
                  <b>
                    {((data.generation.memory_ids as unknown[]) || []).length}
                  </b>
                  <span>Memories provided</span>
                </div>
                <div>
                  <b>{String(data.generation.recent_turns)}</b>
                  <span>Recent exchanges</span>
                </div>
              </div>
            )}
            <p className="helper">
              These are the saved inputs for this reply, including retrieved
              evidence. They are not hidden model reasoning.
            </p>
            {data.context ? (
              <details className="code-details">
                <summary>Exact context sent to the reply model</summary>
                <pre tabIndex={0}>{JSON.stringify(data.context, null, 2)}</pre>
              </details>
            ) : (
              <p className="notice">
                No context snapshot was saved for this turn.
              </p>
            )}
            {data.generation && (
              <details className="code-details">
                <summary>Generation metadata</summary>
                <pre tabIndex={0}>
                  {JSON.stringify(data.generation, null, 2)}
                </pre>
              </details>
            )}
            <button
              className="secondary"
              onClick={() => downloadJson(data, "turn-" + turnId + ".json")}
            >
              <Download size={15} />
              Download this record
            </button>
          </>
        )
      )}
    </>
  );
}
