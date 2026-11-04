import { useMemo, useRef, useState } from "react";
import { cx } from "../fmt.js";
import { StepHead } from "./ui.jsx";

export default function ScriptPicker({ scripts, counts, onAdd, onNewScript, onEditScript,
                                       onImport, canEditScripts = true, me = null,
                                       onShare = null, onUnshare = null }) {
  const [cat, setCat] = useState("");
  const fileInput = useRef(null);
  const categories = useMemo(
    () => [...new Set(scripts.map((s) => s.category).filter(Boolean))].sort(),
    [scripts]);
  const shown = cat ? scripts.filter((s) => s.category === cat) : scripts;

  const pickImport = () => fileInput.current?.click();
  const onImportPicked = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // let picking the same file again re-fire change
    if (file) await onImport(file);
  };

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
          <>
            <a className="btn ghost sm" href="/api/scripts/export" download
               title="Every script in one tar.gz — flat scripts as code + manifest pairs, directory-style scripts under their names. Import takes it back.">
              ⬇ Export all
            </a>
            {canEditScripts && (
              <>
                <button className="btn ghost sm" onClick={pickImport}
                        title="Import script(s) from a .tar.gz — the Export format, or a pack of scripts (a plain .tar works too: Safari's 'open safe files' gunzips downloads)">
                  ⬆ Import
                </button>
                <input ref={fileInput} type="file" accept=".gz,.tgz,.tar" hidden
                       onChange={onImportPicked} />
                <button className="btn ghost sm" onClick={onNewScript}
                        title="Create a new script in the browser editor">
                  ＋ New script
                </button>
              </>
            )}
            {!canEditScripts && (
              <span className="muted sm" title="Script authoring is disabled on this server">
                🔒 editing disabled
              </span>
            )}
          </>
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
              {s.owner && (
                <span className="chip" title={me && s.owner === me.username
                  ? "Your private script — visible only to you and admins. Share it to let everyone run it."
                  : `${s.owner}'s private script`}>
                  {me && s.owner === me.username ? "🔒 private" : `🔒 ${s.owner}`}
                </span>
              )}
              {s.error && <span className="badge failed">broken</span>}
              {onShare && s.owner && (me?.role === "admin" || me?.username === s.owner) && (
                <button
                  className="icon-btn"
                  title="Move this script into the shared library — every user will see and run it"
                  onClick={(e) => { e.stopPropagation(); onShare(s); }}
                >⇪</button>
              )}
              {onUnshare && !s.owner && me?.role === "admin" && (
                <button
                  className="icon-btn"
                  title="Pull this script out of the shared library, into your private scripts"
                  onClick={(e) => { e.stopPropagation(); onUnshare(s); }}
                >⇩</button>
              )}
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
