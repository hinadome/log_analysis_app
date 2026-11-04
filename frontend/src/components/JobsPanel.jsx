import { cx, elapsed, fmtBytes, fmtTime } from "../fmt.js";
import { StatusBadge } from "./ui.jsx";

const ACTIVE = new Set(["queued", "waiting", "running"]);

const STATUS_FILTERS = [
  { value: "", label: "All" },
  { value: "queued,waiting,running", label: "Active" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled,skipped", label: "Cancelled" },
];

export default function JobsPanel({ jobs, total, onLoadMore, query, onSearch, status, onStatus, onOpen, onCancel, onDelete, onClearFinished, onPrune, dataSize }) {
  const searching = Boolean(query && query.trim());
  const shown = jobs; // filtering + paging happen server-side (?q=, ?limit=)
  const hasFinished = jobs.some((j) => !ACTIVE.has(j.status));
  const more = total > jobs.length;
  return (
    <aside className="jobs-panel">
      <div className="jobs-head">
        <h2>
          Jobs <span className="muted">({jobs.length}{more ? ` of ${total}` : ""})</span>
        </h2>
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
        onChange={(e) => onSearch(e.target.value)}
      />
      <div className="status-chips" role="group" aria-label="Filter by status">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.label}
            className={cx("chip", status === f.value && "on")}
            onClick={() => onStatus(f.value)}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="jobs-list">
        {shown.length === 0 && (
          <div className="empty">
            {searching
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
                {j.label ? `${j.label} · ` : ""}{j.chain_id ? `step ${j.chain_index + 1}/${j.chain_size} · ` : ""}{j.log_name || "—"}
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
      {more && (
        <button className="btn ghost load-more" onClick={onLoadMore}>
          Load more — showing {jobs.length} of {total}
        </button>
      )}
      {dataSize != null && (
        <div className="jobs-foot muted sm">
          storage: {fmtBytes(dataSize)} in job history
        </div>
      )}
    </aside>
  );
}
