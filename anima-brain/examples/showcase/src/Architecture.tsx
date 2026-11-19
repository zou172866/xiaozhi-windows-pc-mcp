import { useEffect, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import {
  ArrowLeft,
  ArrowRight,
  Play,
  Pause,
  RotateCcw,
  ExternalLink,
  Layers,
  Search,
  RefreshCw,
  Heart,
  SlidersHorizontal,
} from "lucide-react";
import {
  atlasContent,
  atlasLanguage,
  type Translate,
} from "./architectureLocale";
import "./architecture.css";

const icons = [Layers, Search, RefreshCw, Heart];
const tones = {
  neutral: {
    fill: "#fffdf7",
    stroke: "#b9b3a3",
    active: "#e9e7db",
    ink: "#4c5343",
  },
  memory: {
    fill: "#f0f2e8",
    stroke: "#a5af91",
    active: "#e0e7d1",
    ink: "#44573c",
  },
  emotion: {
    fill: "#f8eee5",
    stroke: "#c0a28e",
    active: "#f0dac8",
    ink: "#895335",
  },
};

function Slider({
  label,
  value,
  min,
  max,
  step = 1,
  unit = "",
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  unit?: string;
  onChange: (v: number) => void;
}) {
  return (
    <label className="atlas-slider">
      <span>
        {label}
        <output>
          {value}
          {unit}
        </output>
      </span>
      <input
        type="range"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

function RetrievalLab({ t }: { t: Translate }) {
  const [age, setAge] = useState(14),
    [importance, setImportance] = useState(7),
    [arousal, setArousal] = useState(0.5),
    [days, setDays] = useState(1),
    [dated, setDated] = useState(true);
  const [lex, setLex] = useState(1),
    [vector, setVector] = useState(4),
    [vectors, setVectors] = useState(true);
  const decay = Math.exp(-age / (14 + 30 * arousal));
  const base = importance * (1 + arousal) * decay,
    urgency = dated ? 2 / (1 + Math.abs(days)) : 0;
  const score = base + urgency;
  return (
    <section className="atlas-lab" aria-label={t("Retrieval formula explorer")}>
      <div className="atlas-section-heading">
        <span className="arch-kicker">{t("OPEN THE EQUATION")}</span>
        <h2>{t("Two scores, two purposes.")}</h2>
        <p>
          {t(
            "These calculate the current Python formulas on illustrative inputs. They do not query your memories.",
          )}
        </p>
      </div>
      <div className="atlas-equations">
        <article className="atlas-equation">
          <div className="atlas-card-heading">
            <span className="atlas-number">01</span>
            <h3>{t("Evidence relevance")}</h3>
            <span className="atlas-badge">RRF</span>
          </div>
          <p className="atlas-formula">
            Σ <span>1 / (40 + rank)</span>
          </p>
          <p>
            {t(
              "Each available ranking channel adds a vote. The example uses keyword and optional vector ranks.",
            )}
          </p>
          <Slider
            label={t("Keyword rank")}
            value={lex}
            min={1}
            max={60}
            onChange={setLex}
          />
          <Slider
            label={t("Vector rank")}
            value={vector}
            min={1}
            max={60}
            onChange={setVector}
          />
          <label className="atlas-check">
            <input
              type="checkbox"
              checked={vectors}
              onChange={(e) => setVectors(e.target.checked)}
            />{" "}
            {t("Include vector match")}
          </label>
          <div className="atlas-score">
            <span>
              {t("Fused relevance")}
              <small>{t("Higher ranks first within this search pool")}</small>
            </span>
            <output data-testid="rrf-score">
              {(1 / (40 + lex) + (vectors ? 1 / (40 + vector) : 0)).toFixed(5)}
            </output>
          </div>
          <p className="atlas-fine">
            {t(
              "Evidence uses RRF; archive memories also use recency and importance.",
            )}
          </p>
          <code>memory.py · recall()</code>
          <p className="atlas-fine">{t("Archive: 0.6 relevance + 0.25 exp(-age/45) + 0.15 importance/10")}</p>
        </article>
        <article className="atlas-equation">
          <div className="atlas-card-heading">
            <span className="atlas-number">02</span>
            <h3>{t("Open-loop priority")}</h3>
            <span className="atlas-badge clay">{t("TIME-AWARE")}</span>
          </div>
          <p className="atlas-formula">
            I (1 + A) e<sup>−t / (14 + 30A)</sup> + U
          </p>
          <p>
            {t(
              "I = importance, A = arousal, t = days since update. U = 2 / (1 + |days to event|), or 0 without a date.",
            )}
          </p>
          <div className="atlas-slider-grid">
            <Slider
              label={t("Importance")}
              value={importance}
              min={1}
              max={10}
              onChange={setImportance}
            />
            <Slider
              label={t("Arousal")}
              value={arousal}
              min={0}
              max={1}
              step={0.05}
              onChange={setArousal}
            />
            <Slider
              label={t("Age since update")}
              value={age}
              min={0}
              max={90}
              unit={t("d")}
              onChange={setAge}
            />
            <Slider
              label={t("Days to event")}
              value={days}
              min={-30}
              max={30}
              unit={t("d")}
              onChange={setDays}
            />
          </div>
          <label className="atlas-check">
            <input
              type="checkbox"
              checked={dated}
              onChange={(e) => setDated(e.target.checked)}
            />{" "}
            {t("Has an event date")}
          </label>
          <div className="atlas-score">
            <span>
              {t("Follow-up priority")}
              <small>
                {base.toFixed(2)} {t("aged importance +")} {urgency.toFixed(2)}{" "}
                {t("urgency")}
              </small>
            </span>
            <output data-testid="loop-score">{score.toFixed(2)}</output>
          </div>
          <div className="atlas-meter" aria-hidden="true">
            <span style={{ width: `${(base / 22) * 100}%` }} />
            <i style={{ width: `${(urgency / 22) * 100}%` }} />
          </div>
          <p className="atlas-fine">
            {t(
              "Top 12 eligible open items; higher arousal slows age decay. This queue supplies context, not automatic outreach.",
            )}
          </p>
          <code>memory.py · loops()</code>
        </article>
      </div>
      <aside className="atlas-callout">
        <SlidersHorizontal size={20} />
        <p>
          <strong>
            {t("Context assembly adds priorities beyond ranking.")}
          </strong>{" "}
          {t(
            "Pinned boundaries, revised facts, core notes and recent conversation have their own inclusion rules. Observation dates can filter original evidence; event dates and fact validity are handled separately.",
          )}
        </p>
      </aside>
    </section>
  );
}

function EmotionLab({ t }: { t: Translate }) {
  const [hours, setHours] = useState(12);
  const mood = Math.pow(2, -hours / 12),
    tension = Math.pow(2, -hours / 72);
  const path = (half: number) =>
    Array.from(
      { length: 73 },
      (_, i) =>
        `${i ? "L" : "M"}${42 + i * 6.5} ${207 - Math.pow(2, (-i * 2) / half) * 164}`,
    ).join(" ");
  return (
    <section className="atlas-lab" aria-label={t("Emotion decay explorer")}>
      <div className="atlas-section-heading">
        <span className="arch-kicker">{t("TIME IS PART OF THE STATE")}</span>
        <h2>{t("A feeling fades. History stays.")}</h2>
        <p>
          {t(
            "Isolated decay, with no new messages or daily weather. These are character-writing controls, not measurements of human emotion.",
          )}
        </p>
      </div>
      <div className="atlas-emotion-grid">
        <article className="atlas-equation">
          <h3>{t("Different half-lives")}</h3>
          <svg
            className="atlas-decay-chart"
            viewBox="0 0 550 265"
            role="img"
            aria-label={t(
              "PAD deviation halves every 12 hours. Tension halves every 72 hours. Both approach zero over elapsed time.",
            )}
          >
            {[0, 0.5, 1].map((v) => (
              <g key={v}>
                <line
                  x1="42"
                  x2="510"
                  y1={207 - v * 164}
                  y2={207 - v * 164}
                  stroke="#d8d3c6"
                />
                <text x="2" y={211 - v * 164}>
                  {v * 100}%
                </text>
              </g>
            ))}
            {[0, 24, 48, 72, 96, 120, 144].map((v) => (
              <text key={v} x={42 + v * 3.25} y="238" textAnchor="middle">
                {v}
                {t("h")}
              </text>
            ))}
            <path d={path(12)} fill="none" stroke="#536548" strokeWidth="3" />
            <path
              d={path(72)}
              fill="none"
              stroke="#9b6247"
              strokeWidth="3"
              strokeDasharray="6 4"
            />
            <line
              x1={42 + hours * 3.25}
              x2={42 + hours * 3.25}
              y1="30"
              y2="207"
              stroke="#665f51"
              strokeDasharray="2 5"
            />
            <circle
              cx={42 + hours * 3.25}
              cy={207 - mood * 164}
              r="5"
              fill="#536548"
            />
            <circle
              cx={42 + hours * 3.25}
              cy={207 - tension * 164}
              r="5"
              fill="#9b6247"
            />
          </svg>
          <Slider
            label={t("Hours since state update")}
            value={hours}
            min={0}
            max={144}
            unit={t("h")}
            onChange={setHours}
          />
          <dl className="atlas-readouts">
            <div>
              <dt>{t("PAD deviation remaining")}</dt>
              <dd data-testid="pad-remaining">{(mood * 100).toFixed(1)}%</dd>
            </div>
            <div>
              <dt>{t("Tension remaining")}</dt>
              <dd data-testid="tension-remaining">
                {(tension * 100).toFixed(1)}%
              </dd>
            </div>
          </dl>
          <p className="atlas-formula small">
            PAD(t) = b + (PAD₀ − b) × 2<sup>−h/12</sup>
          </p>
          <p className="atlas-formula small">
            tension(t) = tension₀ × 2<sup>−h/72</sup>
          </p>
          <p className="atlas-fine">
            {t(
              "PAD baseline b = (0.15, −0.15, 0.05). Pleasure, arousal and dominance relax toward that baseline.",
            )}
          </p>
          <code>emotion.py · decayed()</code>
        </article>
        <article className="atlas-rulebook">
          <h3>{t("What happens when a new turn arrives?")}</h3>
          <ol>
            <li>
              <strong>{t("Appraise the moment")}</strong>
              <p>
                {t(
                  "12 emotion targets. PAD pull = 0.3 × intensity / 3, applied toward the selected target.",
                )}
              </p>
            </li>
            <li>
              <strong>{t("Update five relationship axes")}</strong>
              <p>
                {t(
                  "Trust, warmth, familiarity, tension and romance. Repeated positive warmth is reduced; trust losses are amplified.",
                )}
              </p>
            </li>
            <li>
              <strong>{t("Apply boundaries and pacing")}</strong>
              <p>
                {t(
                  "Each axis is clamped to [0, 1], with total absolute movement capped at 0.12 per UTC day. Romance growth requires opt-in and familiarity > 0.2.",
                )}
              </p>
            </li>
            <li>
              <strong>{t("Carry a stable description forward")}</strong>
              <p>
                {t(
                  "Band hysteresis of 0.035 reduces abrupt label changes. After 7 absent days, excess warmth cools toward 0.6 × familiarity over a 35-day half-life.",
                )}
              </p>
            </li>
          </ol>
          <code>emotion.py · advance() / _bands()</code>
        </article>
      </div>
    </section>
  );
}

function MemoryLayers({ t }: { t: Translate }) {
  return (
    <section className="atlas-lab">
      <div className="atlas-section-heading">
        <span className="arch-kicker">
          {t("ONE HISTORY, DISTINCT REPRESENTATIONS")}
        </span>
        <h2>{t("Knowing a fact is different from remembering a moment.")}</h2>
      </div>
      <div className="atlas-layer-grid">
        <article>
          <span className="atlas-badge">{t("SEMANTIC")}</span>
          <h3>{t("What is true now?")}</h3>
          <p>
            {t(
              "Current assertions, preferences and boundaries, with subject, source quote, revision and temporal details.",
            )}
          </p>
          <blockquote>
            {t("Interview: Monday.")}
            <br />
            {t("Preference: skip pep talks today.")}
          </blockquote>
          <small>
            {t(
              "A correction changes current knowledge while preserving its revision history.",
            )}
          </small>
        </article>
        <article>
          <span className="atlas-badge">{t("EPISODIC")}</span>
          <h3>{t("What happened together?")}</h3>
          <p>
            {t(
              "Derived accounts of events and exchanges, tied to the original evidence by source IDs.",
            )}
          </p>
          <blockquote>
            {t(
              "We practised for the interview; later, the user rescheduled it.",
            )}
          </blockquote>
          <small>
            {t(
              "Summaries are interpretations. Inspect the original when details matter.",
            )}
          </small>
        </article>
        <article>
          <span className="atlas-badge clay">{t("EMOTIONAL CONTINUITY")}</span>
          <h3>{t("How does the character carry it?")}</h3>
          <p>
            {t(
              "PAD and relationship state guide the next reply. Recent attunement records describe cues and feedback separately.",
            )}
          </p>
          <blockquote>
            {t("Keep the tone light, familiar and unpressured.")}
          </blockquote>
          <small>
            {t(
              "Character state is not a stored diagnosis or a factual claim about the user.",
            )}
          </small>
        </article>
      </div>
      <p className="atlas-caption">
        {t(
          "Illustrative representations of one conversation. Source evidence underlies memory; core notes are a compact synthesis, not a third kind of ground truth.",
        )}
      </p>
    </section>
  );
}

function ConsolidationNotes({ t }: { t: Translate }) {
  return (
    <section className="atlas-lab">
      <div className="atlas-section-heading">
        <span className="arch-kicker">{t("THE CYCLE, IN PRACTICE")}</span>
        <h2>{t("Keep the moment. Update the meaning.")}</h2>
      </div>
      <div className="atlas-layer-grid">
        <article>
          <span className="atlas-badge">{t("01 / BEFORE")}</span>
          <h3>{t("A Friday plan")}</h3>
          <p>
            {t(
              "An original message and a cited fact say the interview is on Friday. A previous episode and core may mention it.",
            )}
          </p>
          <blockquote>{t("“The interview’s on Friday.”")}</blockquote>
        </article>
        <article>
          <span className="atlas-badge clay">{t("02 / CHANGE")}</span>
          <h3>{t("A Monday correction")}</h3>
          <p>
            {t(
              "Commit the new statement and the permitted fact update. Invalidate the core and documents citing the changed source.",
            )}
          </p>
          <blockquote>{t("“They moved it to Monday.”")}</blockquote>
        </article>
        <article>
          <span className="atlas-badge">{t("03 / AFTER")}</span>
          <h3>{t("Continuity without contradiction")}</h3>
          <p>
            {t(
              "Rebuild cited summaries from the updated snapshot. Retain both originals with knowledge-update markers, and refresh affected indexes.",
            )}
          </p>
          <blockquote>
            {t("Current plan: Monday.")}
            <br />
            {t("Historical plan: Friday.")}
          </blockquote>
        </article>
      </div>
      <aside className="atlas-callout">
        <RefreshCw size={20} />
        <p>
          <strong>{t("Due does not mean already completed.")}</strong>{" "}
          {t(
            "Hosts without an explicit policy request host summaries; SDK-created characters start with maintenance disabled by default. An active host must prepare and commit host summaries. Background model summaries are an explicit alternative. Optional embedding jobs require a configured provider and running worker.",
          )}
        </p>
      </aside>
      <p className="atlas-caption">
        {t(
          "Source: automation.py · host_memory.py · memory.py · engine.py. Thresholds shown above are configurable defaults, not a universal schedule.",
        )}
      </p>
    </section>
  );
}

export default function Architecture() {
  const [language, setLanguage] = useState(() =>
    atlasLanguage(window.location.search),
  );
  const { t, flow } = atlasContent(language);
  useEffect(() => {
    const previous = document.documentElement.lang;
    document.documentElement.lang = language;
    return () => {
      document.documentElement.lang = previous;
    };
  }, [language]);
  useEffect(() => {
    const sync = () => setLanguage(atlasLanguage(window.location.search));
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const [chapter, setChapter] = useState(0),
    [step, setStep] = useState(0),
    [playing, setPlaying] = useState(false);
  const reduce = useReducedMotion();
  const view = flow.views[chapter],
    stage = view.steps[step];
  useEffect(() => {
    if (!playing || reduce) return;
    const t = setInterval(
      () => setStep((s) => (s + 1) % view.steps.length),
      6500,
    );
    return () => clearInterval(t);
  }, [playing, reduce, view.steps.length]);
  useEffect(() => {
    if (reduce) setPlaying(false);
  }, [reduce]);
  const choose = (i: number) => {
    setPlaying(false);
    setStep(0);
    setChapter(i);
  };
  return (
    <main className="architecture" lang={language}>
      <header className="arch-nav">
        <a href="/">
          ANIMA <span>/ COMPANION ENGINE</span>
        </a>
        <nav
          className="atlas-languages"
          aria-label={t("Architecture language")}
        >
          {(["en", "zh-CN"] as const).map((locale) => (
            <a
              key={locale}
              href={"?view=architecture&lang=" + locale}
              lang={locale}
              hrefLang={locale}
              aria-current={language === locale ? "true" : undefined}
              onClick={(event) => {
                if (
                  event.button !== 0 ||
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                )
                  return;
                event.preventDefault();
                const url = new URL(window.location.href);
                url.searchParams.set("lang", locale);
                window.history.pushState(null, "", url);
                setLanguage(locale);
              }}
            >
              {locale === "en" ? "English" : "简体中文"}
            </a>
          ))}
        </nav>
        <a
          className="atlas-origin"
          href="https://anima-lab.io"
          target="_blank"
          rel="noreferrer"
        >
          {t("From Anima Lab")}
          <ExternalLink size={13} />
        </a>
      </header>
      <section className="arch-intro">
        <div>
          <span className="arch-kicker">
            {t("THE BRAIN ATLAS / AN INTERACTIVE FIELD GUIDE")}
          </span>
          <h1>
            {t("A moment becomes")}
            <br />
            <em>{t("continuity.")}</em>
          </h1>
        </div>
        <div className="atlas-intro-aside">
          <p>
            {t(
              "Inside the memory and emotion systems that make the next conversation feel connected.",
            )}
          </p>
          <span>
            {t("Originals → knowledge → shared history → the next turn")}
          </span>
        </div>
      </section>
      <nav
        data-testid="atlas-chapters"
        className="atlas-chapters"
        aria-label={t("Architecture views")}
      >
        {flow.views.map((v, i) => {
          const Icon = icons[i];
          return (
            <button
              key={v.id}
              onClick={() => choose(i)}
              aria-pressed={chapter === i}
            >
              <span>0{i + 1}</span>
              <Icon size={17} />
              {v.label}
              <ArrowRight className="atlas-chapter-arrow" size={16} />
            </button>
          );
        })}
      </nav>
      <section
        className="arch-map"
        aria-label={t("Companion brain architecture")}
      >
        <div className="arch-map-heading">
          <div>
            <span>{view.eyebrow}</span>
            <h2>{view.description}</h2>
          </div>
          <div className="atlas-legend">
            <span>
              <i />
              {t("Memory")}
            </span>
            <span>
              <i />
              {t("Emotion")}
            </span>
            <span>{t("Illustrative flow")}</span>
          </div>
        </div>
        <p className="atlas-scroll-hint">
          {t("Scroll sideways to explore the full diagram →")}
        </p>
        <div
          className="arch-svg-scroll"
          tabIndex={0}
          role="region"
          aria-label={t("Scrollable architecture diagram")}
        >
          <svg
            viewBox={`0 0 ${flow.width} ${flow.height}`}
            role="img"
            aria-label={`${view.label}: ${view.description}. ${stage.text}`}
          >
            <defs>
              <pattern
                id="atlas-dots"
                width="24"
                height="24"
                patternUnits="userSpaceOnUse"
              >
                <circle cx="1" cy="1" r="0.7" fill="#cbc7b9" opacity="0.45" />
              </pattern>
              <marker
                id="atlas-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="5"
                markerHeight="5"
                orient="auto"
              >
                <path d="M0 0L10 5L0 10" fill="#8b8c7b" />
              </marker>
            </defs>
            <rect
              width={flow.width}
              height={flow.height}
              fill="url(#atlas-dots)"
            />
            {chapter === 0 && (
              <g>
                <text x="30" y="45" className="atlas-lane">
                  {t("THE CONVERSATION")}
                </text>
                <text x="30" y="278" className="atlas-lane">
                  {t("MEMORY / EVERY DERIVED RECORD KEEPS ITS SOURCES")}
                </text>
                <text x="30" y="489" className="atlas-lane">
                  {t("CONTINUITY LOOPS")}
                </text>
              </g>
            )}
            {view.edges.map((e, i) => (
              <g key={chapter + "-" + i}>
                <path
                  d={e.d}
                  fill="none"
                  stroke="#c1c2b2"
                  strokeWidth="1.6"
                  markerEnd="url(#atlas-arrow)"
                />
                {e.stages.includes(step) && (
                  <g key={chapter + "-" + step + "-" + i}>
                    <motion.path
                      d={e.d}
                      fill="none"
                      stroke="#9b6446"
                      strokeWidth="2.5"
                      initial={{ pathLength: reduce ? 1 : 0 }}
                      animate={{ pathLength: 1 }}
                      transition={{ duration: 1.2, ease: "easeInOut" }}
                    />
                    {!reduce && (
                      <circle r="4" fill="#965c3c">
                        <animateMotion
                          dur="2.2s"
                          path={e.d}
                          repeatCount={playing ? "indefinite" : 1}
                          fill="freeze"
                        />
                      </circle>
                    )}
                  </g>
                )}
              </g>
            ))}
            {view.nodes.map((n) => {
              const active = stage.active.includes(n.id),
                tone = tones[n.tone as keyof typeof tones];
              return (
                <g key={n.id}>
                  <motion.rect
                    x={n.x}
                    y={n.y}
                    width={n.w}
                    height={n.h}
                    rx="11"
                    animate={{
                      fill: active ? tone.active : tone.fill,
                      stroke: active ? tone.ink : tone.stroke,
                      strokeWidth: active ? 2 : 1,
                    }}
                    transition={{ duration: reduce ? 0 : 0.35 }}
                  />
                  <text
                    x={n.x + 16}
                    y={n.y + 23}
                    className="atlas-node-tag"
                    fill={tone.ink}
                  >
                    {n.tag}
                  </text>
                  <text x={n.x + 16} y={n.y + 51} className="atlas-node-title">
                    {n.title}
                  </text>
                  <text x={n.x + 16} y={n.y + 76} className="atlas-node-sub">
                    {n.subtitle}
                  </text>
                  {active && (
                    <circle
                      cx={n.x + n.w - 15}
                      cy={n.y + 20}
                      r="3"
                      fill={tone.ink}
                    />
                  )}
                </g>
              );
            })}
          </svg>
        </div>
        <div className="atlas-live-caption">
          <span className="atlas-caption-number">
            {String(step + 1).padStart(2, "0")}
          </span>
          <div key={chapter + "-" + step}>
            <h3>{stage.title}</h3>
            <p>{stage.text}</p>
          </div>
        </div>
        <div className="arch-controls">
          <button
            aria-label={t(playing ? "Pause animation" : "Play animation")}
            onClick={() => setPlaying(!playing)}
            disabled={!!reduce}
          >
            {playing ? <Pause size={16} /> : <Play size={16} />}
            <span>{t(playing ? "Pause" : "Play the flow")}</span>
          </button>
          <button
            aria-label={t("Restart explanation")}
            onClick={() => {
              setStep(0);
              setPlaying(false);
            }}
          >
            <RotateCcw size={16} />
          </button>
          <span>
            {String(step + 1).padStart(2, "0")} /{" "}
            {String(view.steps.length).padStart(2, "0")}
          </span>
          <button
            aria-label={t("Previous step")}
            onClick={() => {
              setPlaying(false);
              setStep((step + view.steps.length - 1) % view.steps.length);
            }}
          >
            <ArrowLeft size={17} />
          </button>
          <button
            data-testid="atlas-next"
            aria-label={t("Next step")}
            onClick={() => {
              setPlaying(false);
              setStep((step + 1) % view.steps.length);
            }}
          >
            <ArrowRight size={17} />
          </button>
        </div>
      </section>
      <section className="atlas-explanation" aria-label={t("Step details")}>
        <nav aria-label={t("Architecture stages")}>
          {view.steps.map((s, i) => (
            <button
              key={s.label}
              aria-current={step === i ? "step" : undefined}
              onClick={() => {
                setPlaying(false);
                setStep(i);
              }}
            >
              <span>0{i + 1}</span>
              {s.label}
            </button>
          ))}
        </nav>
        <div className="atlas-example">
          <span className="arch-kicker">
            {t("ONE CONVERSATION, FOLLOWED THROUGH")}
          </span>
          <blockquote>{stage.example}</blockquote>
          <p>{stage.detail}</p>
        </div>
      </section>
      {chapter === 0 ? (
        <MemoryLayers t={t} />
      ) : chapter === 1 ? (
        <RetrievalLab t={t} />
      ) : chapter === 2 ? (
        <ConsolidationNotes t={t} />
      ) : (
        <EmotionLab t={t} />
      )}
      <footer className="arch-footer">
        <p>
          {t(
            "Based on this Python implementation. Optional features depend on configuration.",
          )}
          <br />
          {t(
            "This walkthrough makes no model calls and reads no private conversations.",
          )}
        </p>
        <a href="/">
          {t("Back to Companion Studio")}
          <ArrowRight size={14} />
        </a>
      </footer>
    </main>
  );
}
