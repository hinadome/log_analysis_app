import { useState } from "react";

export function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

export function StatusBadge({ status }) {
  return (
    <span className={`badge ${status}`}>
      <span className="dot" />
      {status}
    </span>
  );
}

export function Modal({ title, onClose, children, wide }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className={cx("modal", wide && "wide")} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="mono">{title}</span>
          <button className="btn ghost sm" onClick={onClose}>Close ✕</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function StepHead({ n, title, hint, action }) {
  return (
    <div className="step-head">
      <span className="step-num">{n}</span>
      <span className="step-title">{title}</span>
      {hint && <span className="step-hint">{hint}</span>}
      {action && <span className="head-action">{action}</span>}
    </div>
  );
}

/** Rows that can render as a table: a non-empty JSON array of objects. */
function tableRows(parsed) {
  if (!Array.isArray(parsed) || parsed.length === 0) return null;
  if (!parsed.every((r) => r && typeof r === "object" && !Array.isArray(r))) return null;
  return parsed;
}

function TableCell({ value }) {
  if (value === null) return <span className="muted">null</span>;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Render stdout: array-of-objects JSON becomes a table (toggleable),
 * other JSON is pretty-printed, plain text stays plain. */
export function OutputView({ text, className }) {
  const trimmed = (text || "").trim();
  const [mode, setMode] = useState("table");
  let parsed = null;
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      /* plain text */
    }
  }
  const rows = parsed ? tableRows(parsed) : null;
  const columns = rows ? [...new Set(rows.flatMap((r) => Object.keys(r)))] : [];

  return (
    <div className={cx("output-wrap", className)}>
      {rows && (
        <div className="preview-toolbar">
          <button
            className={cx("btn ghost sm", mode === "table" && "active")}
            onClick={() => setMode("table")}
          >▦ Table</button>
          <button
            className={cx("btn ghost sm", mode === "raw" && "active")}
            onClick={() => setMode("raw")}
          >⌗ Raw</button>
          <span className="muted sm">{rows.length} rows · {columns.length} columns</span>
        </div>
      )}
      {rows && mode === "table" ? (
        <div className="table-scroll">
          <table className="json-table">
            <thead>
              <tr>{columns.map((c) => <th key={c} className="mono">{c}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {columns.map((c) => <td key={c}><TableCell value={r[c]} /></td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <pre className="output">{parsed ? JSON.stringify(parsed, null, 2) : (trimmed || "(no output)")}</pre>
      )}
    </div>
  );
}
