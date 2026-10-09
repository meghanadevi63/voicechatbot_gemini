import { CloseIcon, LogoMark, PlusIcon, RefreshIcon } from "./icons.jsx";

const STATUS = {
  checking: { label: "Checking backend…", tone: "neutral" },
  ok: { label: "Backend connected", tone: "ok" },
  down: { label: "Backend unreachable", tone: "bad" },
};

function IngestResult({ ingest }) {
  if (ingest.status === "running") {
    return <p className="note">Indexing documents. This can take a few minutes for large PDFs.</p>;
  }
  if (ingest.status === "error") {
    return <p className="note note-bad">Ingest failed: {ingest.message}</p>;
  }
  if (ingest.status === "done") {
    const { files, pages, chunks } = ingest.result;
    return (
      <div className="note note-ok">
        <p>
          Indexed {chunks} chunks from {pages} pages.
        </p>
        <ul className="file-list">
          {files.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      </div>
    );
  }
  return null;
}

const ANSWER_MODES = [
  { value: "agent", label: "Gemini answers", hint: "Gemini searches the documents and answers. Fastest." },
  {
    value: "rag",
    label: "Read RAG answer",
    hint: "Gemini reads the same answer typed chat gives. About a second slower.",
  },
];

function Choice({ name, options, value, onChange }) {
  return (
    <div className="choice" role="radiogroup">
      {options.map((o) => (
        <label key={o.value} className={value === o.value ? "on" : ""}>
          <input
            type="radio"
            name={name}
            value={o.value}
            checked={value === o.value}
            onChange={() => onChange(o.value)}
          />
          <span className="choice-label">{o.label}</span>
          <span className="choice-hint">{o.hint}</span>
        </label>
      ))}
    </div>
  );
}

export default function Sidebar({
  open,
  onClose,
  backend,
  ingest,
  onIngest,
  onClear,
  canClear,
  voiceSettings,
  onVoiceSettings,
}) {
  const status = STATUS[backend];
  const ingesting = ingest.status === "running";

  return (
    <>
      <div className={`scrim ${open ? "show" : ""}`} onClick={onClose} />
      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="sidebar-head">
          <div className="brand">
            <LogoMark />
            <span>Docs Assistant</span>
          </div>
          <button className="icon-btn sidebar-close" onClick={onClose} aria-label="Close menu">
            <CloseIcon />
          </button>
        </div>

        <button className="btn btn-primary" onClick={onClear} disabled={!canClear}>
          <PlusIcon /> New chat
        </button>

        <section className="sidebar-section">
          <h2>Status</h2>
          <div className={`status status-${status.tone}`}>
            <span className="dot" />
            {status.label}
          </div>
        </section>

        <section className="sidebar-section">
          <h2>Voice answers</h2>
          <Choice
            name="voice-answer"
            options={ANSWER_MODES}
            value={voiceSettings.answer}
            onChange={(answer) => onVoiceSettings({ answer })}
          />
        </section>

        <section className="sidebar-section">
          <h2>Documents</h2>
          <p className="muted">
            Re-index the PDFs in the <code>docs/</code> folder after adding or changing files.
          </p>
          <button className="btn" onClick={onIngest} disabled={ingesting || backend !== "ok"}>
            <RefreshIcon /> {ingesting ? "Indexing…" : "Re-ingest documents"}
          </button>
          <IngestResult ingest={ingest} />
        </section>
      </aside>
    </>
  );
}
