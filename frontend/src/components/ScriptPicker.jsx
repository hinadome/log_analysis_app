import { useMemo, useState } from "react";
import { cx } from "../fmt.js";
import { StepHead } from "./ui.jsx";

export default function ScriptPicker({ scripts, counts, onAdd, onNewScript, onEditScript, canEditScripts = true }) {
  const [cat, setCat] = useState("");
  const categories = useMemo(
    () => [...new Set(scripts.map((s) => s.category).filter(Boolean))].sort(),
    [scripts]);
  const shown = cat ? scripts.filter((s) => s.category === cat) : scripts;

  return (
    <section className="card step">
      <StepHead
        n={2}
        title="Analysis scripts"
        hint={scripts.length
          ? cat
            ? `${shown.length} of ${scripts.length} in “${cat}” · click to add`
            : `${scripts.length} available · click to add`
          : "No scripts found in scripts/"}
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
      {categories.length > 0 && (
        <div className="cat-chips">
          <button className={cx("chip chip-btn", !cat && "on")} onClick={() => setCat("")}
                  title="Show every script">all</button>
          {categories.map((c) => (
            <button key={c} className={cx("chip chip-btn", cat === c && "on")}
                    onClick={() => setCat(c)}>
              {c}
            </button>
          ))}
        </div>
      )}
      <div className="script-grid">
        {shown.map((s) => (
          <div key={s.id} className="script-card" onClick={() => onAdd(s)}>
            <div className="script-card-head">
              <span className="checkbox add">+</span>
              <span className="script-name">{s.name}</span>
              {s.category && <span className="chip">{s.category}</span>}
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
              {!s.needs_log && <span className="chip accent" title="The command has no {log} — runs without a selected log">no log needed</span>}
            </div>
            <code className="script-cmd mono">$ {s.command}</code>
          </div>
        ))}
      </div>
    </section>
  );
}
