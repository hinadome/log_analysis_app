import { useMemo, useState } from "react";

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
export function tableRows(parsed) {
  if (!Array.isArray(parsed) || parsed.length === 0) return null;
  if (!parsed.every((r) => r && typeof r === "object" && !Array.isArray(r))) return null;
  return parsed;
}

/** Sort values the way a human expects: numbers numeric, nulls last, strings locale-aware. */
function compareCells(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
}

function csvEscape(value) {
  if (value == null) return "";
  const s = typeof value === "object" ? JSON.stringify(value) : String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
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
  const [sort, setSort] = useState(null); // {col, dir} — dir 1 asc, -1 desc
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
  const sortedRows = useMemo(() => {
    if (!rows || !sort) return rows;
    const { col, dir } = sort;
    return [...rows].sort((a, b) => compareCells(a[col], b[col]) * dir);
  }, [rows, sort]);

  const toggleSort = (col) => setSort((s) =>
    s && s.col === col ? (s.dir === 1 ? { col, dir: -1 } : null) : { col, dir: 1 });

  const downloadCsv = () => {
    const lines = [columns.map(csvEscape).join(",")];
    for (const r of sortedRows) lines.push(columns.map((c) => csvEscape(r[c])).join(","));
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "output.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

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
          <span className="spacer" />
          <button className="btn ghost sm" onClick={downloadCsv}
                  title="Download the table (sorted as shown) as CSV">⬇ CSV</button>
        </div>
      )}
      {rows && mode === "table" ? (
        <div className="table-scroll">
          <table className="json-table">
            <thead>
              <tr>{columns.map((c) => (
                <th key={c} className="mono sortable" title="Sort"
                    aria-sort={sort?.col === c ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
                    onClick={() => toggleSort(c)}>
                  {c}{sort?.col === c ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
                </th>
              ))}</tr>
            </thead>
            <tbody>
              {sortedRows.map((r, i) => (
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
