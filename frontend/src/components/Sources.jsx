import { useState } from "react";
import { ChevronIcon, FileIcon } from "./icons.jsx";

// translated: the answer isn't in English, while the sources quote the English documents
export default function Sources({ sources, translated }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="sources">
      <button className={`sources-toggle ${open ? "open" : ""}`} onClick={() => setOpen(!open)}>
        <ChevronIcon />
        {sources.length} {sources.length === 1 ? "source" : "sources"}
        {translated && " (English)"}
      </button>

      {open && (
        <ul className="source-list">
          {sources.map((s, i) => (
            <li key={i} className="source-card">
              <details>
                <summary>
                  <FileIcon />
                  <span className="source-name">{s.source || "Unknown document"}</span>
                  {s.page != null && <span className="source-page">p. {s.page}</span>}
                </summary>
                <p className="source-text">{s.content}</p>
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
