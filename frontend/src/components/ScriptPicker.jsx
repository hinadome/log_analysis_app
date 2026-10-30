import { StepHead } from "./ui.jsx";

export default function ScriptPicker({ scripts, counts, onAdd, onNewScript, onEditScript, canEditScripts = true }) {
  return (
    <section className="card step">
      <StepHead
        n={2}
        title="Analysis scripts"
        hint={scripts.length ? `${scripts.length} available · click to add` : "No scripts found in scripts/"}
        action={
          canEditScripts ? (
            <button className="btn ghost sm" onClick={onNewScript}
                    title="Create a new script in the browser editor">
              ＋ New script
            </button>
          ) : (
            <span className="muted sm" title="Script authoring is disabled on this server">
              🔒 editing disabled
            </span>
          )
        }
      />
      <div className="script-grid">
        {scripts.map((s) => (
          <div key={s.id} className="script-card" onClick={() => onAdd(s)}>
            <div className="script-card-head">
              <span className="checkbox add">+</span>
              <span className="script-name">{s.name}</span>
              {counts[s.id] > 0 && <span className="chip accent">×{counts[s.id]}</span>}
              {s.error && <span className="badge failed">broken</span>}
              {canEditScripts && (
                <button
                  className="icon-btn"
                  title={s.error ? "Fix in the editor" : "Edit in the editor"}
                  onClick={(e) => { e.stopPropagation(); onEditScript(s); }}
                >✏️</button>
              )}
            </div>
            <div className="muted sm">{s.error || s.description}</div>
            <div className="script-meta">
              <span className="chip">{s.parameters.length} params</span>
              <span className="chip">{s.timeout}s timeout</span>
              {s.output_filename && <span className="chip accent">file output</span>}
            </div>
            <code className="script-cmd mono">$ {s.command}</code>
          </div>
        ))}
      </div>
    </section>
  );
}
