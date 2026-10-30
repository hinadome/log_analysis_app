import { useState } from "react";
import { cx, elapsed, fmtBytes, fmtTime } from "../fmt.js";
import { StatusBadge } from "./ui.jsx";

const ACTIVE = new Set(["queued", "waiting", "running"]);

export default function JobsPanel({ jobs, onOpen, onCancel, onDelete, onClearFinished, onPrune, dataSize }) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const shown = needle
    ? jobs.filter((j) => `${j.label || ""} ${j.script_name} ${j.script_id} ${j.log_name} ${j.id}`
        .toLowerCase().includes(needle))
    : jobs;
  const hasFinished = jobs.some((j) => !ACTIVE.has(j.status));
  return (
    <aside className="jobs-panel">
      <div className="jobs-head">
        <h2>Jobs <span className="muted">({shown.length}{needle ? ` of ${jobs.length}` : ""})</span></h2>
        <div className="jobs-head-actions">
          {hasFinished && (
            <button className="btn ghost sm" onClick={onClearFinished}>Clear finished</button>
          )}
          <button className="btn ghost sm" title="Delete jobs older than N days" onClick={onPrune}>
            🧹 Prune
          </button>
        </div>
      </div>
      <input
        className="search-input"
        type="search"
        placeholder="Search label, script, log…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="jobs-list">
        {shown.length === 0 && (
          <div className="empty">
            {needle
              ? <>No jobs match your search.</>
              : <>No jobs yet.<br />Pick a log and scripts, then hit Run.</>}
          </div>
        )}
        {shown.map((j) => (
          <div
            key={j.id}
            className={cx("job-row", j.status)}
            onClick={() => onOpen(j.id)}
          >
            <StatusBadge status={j.status} />
            <div className="job-row-main">
              <div className="job-row-title">{j.script_name}</div>
              <div className="job-row-sub mono">
                {j.label ? `${j.label} · ` : ""}{j.chain_id ? `step ${j.chain_index + 1}/${j.chain_size} · ` : ""}{j.log_name}
              </div>
            </div>
            <div className="job-row-side">
              <div className="muted">{fmtTime(j.created_at)}</div>
              <div className="muted">
                {j.status === "running" && <span className="spin">◐</span>}{" "}
                {elapsed(j.started_at || j.created_at, j.finished_at) ?? ""}
              </div>
            </div>
            {ACTIVE.has(j.status) ? (
              <button
                className="btn ghost sm"
                title="Cancel"
                onClick={(e) => { e.stopPropagation(); onCancel(j.id); }}
              >
                ✕
              </button>
            ) : (
              <button
                className="btn ghost sm"
                title="Delete"
                onClick={(e) => { e.stopPropagation(); onDelete(j.id); }}
              >
                🗑
              </button>
            )}
          </div>
        ))}
      </div>
      {dataSize != null && (
        <div className="jobs-foot muted sm">
          storage: {fmtBytes(dataSize)} in job history
        </div>
      )}
    </aside>
  );
}
