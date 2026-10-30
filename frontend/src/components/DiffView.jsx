import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { fmtTime } from "../fmt.js";
import { Modal } from "./ui.jsx";

// Diffing more than this many lines per side is O(n*m) memory; larger
// outputs are truncated to this window with a note.
const MAX_DIFF_LINES = 2000;

/**
 * Unified line diff via LCS. Returns rows of
 * { type: "same" | "add" | "del", text } plus a truncation flag.
 */
export function lineDiff(aText, bText) {
  const fullA = aText.split("\n");
  const fullB = bText.split("\n");
  const truncated = fullA.length > MAX_DIFF_LINES || fullB.length > MAX_DIFF_LINES;
  const a = fullA.slice(0, MAX_DIFF_LINES);
  const b = fullB.slice(0, MAX_DIFF_LINES);
  if (a.at(-1) === "") a.pop();
  if (b.at(-1) === "") b.pop();

  // LCS lengths table (Int32Array keeps 2000x2000 at ~16 MB)
  const n = a.length;
  const m = b.length;
  const width = m + 1;
  const dp = new Int32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * width + j] = a[i] === b[j]
        ? dp[(i + 1) * width + j + 1] + 1
        : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1]);
    }
  }

  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ type: "same", text: a[i] });
      i++; j++;
    } else if (dp[(i + 1) * width + j] >= dp[i * width + j + 1]) {
      rows.push({ type: "del", text: a[i] });
      i++;
    } else {
      rows.push({ type: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) rows.push({ type: "del", text: a[i++] });
  while (j < m) rows.push({ type: "add", text: b[j++] });
  return {
    rows,
    truncated,
    added: rows.filter((r) => r.type === "add").length,
    removed: rows.filter((r) => r.type === "del").length,
    same: rows.filter((r) => r.type === "same").length,
  };
}

export default function DiffView({ jobId, onClose }) {
  const [candidates, setCandidates] = useState(null);
  const [otherId, setOtherId] = useState("");
  const [left, setLeft] = useState(null);
  const [right, setRight] = useState(null);
  const [loading, setLoading] = useState(false);

  // completed jobs to compare against, newest first
  useEffect(() => {
    let cancelled = false;
    api.listJobs({ limit: 500 }).then((r) => {
      if (cancelled) return;
      const done = r.jobs.filter((j) => j.status === "completed" && j.id !== jobId);
      setCandidates(done);
      const job = r.jobs.find((j) => j.id === jobId);
      // default: latest run of the same script, else the latest other job
      const sibling = job && done.find((j) => j.script_id === job.script_id);
      setOtherId((sibling || done[0] || {}).id || "");
    }).catch(() => !cancelled && setCandidates([]));
    return () => { cancelled = true; };
  }, [jobId]);

  // fetch both stdouts whenever a pair is selected
  const pair = [jobId, otherId].join("|");
  useEffect(() => {
    if (!otherId) return undefined;
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.getJobStdout(jobId),
      api.getJobStdout(otherId),
      api.getJob(jobId),
      api.getJob(otherId),
    ]).then(([aText, bText, aJob, bJob]) => {
      if (cancelled) return;
      setLeft({ text: aText, job: aJob });
      setRight({ text: bText, job: bJob });
    }).catch(() => {
      if (!cancelled) { setLeft(null); setRight(null); }
    }).finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pair]);

  const diff = useMemo(
    () => (left && right ? lineDiff(left.text, right.text) : null),
    [left, right],
  );

  const describe = (side) => {
    if (!side?.job) return "…";
    const j = side.job;
    return `${j.script_name}${j.label ? ` · ${j.label}` : ""} · ${fmtTime(j.created_at)}`;
  };

  return (
    <Modal title="⇄ Compare job outputs" onClose={onClose} wide>
      <div className="diff-head">
        <div className="diff-side">
          <div className="muted sm">This job</div>
          <div className="mono sm">{describe(left)}</div>
        </div>
        <div className="diff-side">
          <div className="muted sm">
            Compare with{" "}
            <select
              className="editor-input diff-select"
              value={otherId}
              onChange={(e) => { setLeft(null); setRight(null); setOtherId(e.target.value); }}
            >
              {(candidates || []).length === 0 && <option value="">No other completed jobs</option>}
              {(candidates || []).map((j) => (
                <option key={j.id} value={j.id}>
                  {j.script_name}
                  {j.label ? ` · ${j.label}` : ""}
                  {" · "}{fmtTime(j.created_at)}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {loading && <div className="muted">Loading outputs…</div>}
      {!loading && !otherId && (
        <div className="empty">Run another job first — there's nothing completed to compare with.</div>
      )}
      {!loading && diff && (
        <>
          <div className="diff-summary muted sm">
            <span className="chip okish">+{diff.added} added</span>{" "}
            <span className="chip failed">−{diff.removed} removed</span>{" "}
            <span className="chip">{diff.same} unchanged</span>
            {diff.truncated && (
              <span className="chip warn">only first {MAX_DIFF_LINES} lines per side</span>
            )}
            {diff.added + diff.removed === 0 && <span className="chip accent">identical</span>}
          </div>
          <div className="diff">
            {diff.rows.map((r, i) => (
              <div key={i} className={`diff-line ${r.type}`}>
                <span className="diff-sign">{r.type === "add" ? "+" : r.type === "del" ? "−" : " "}</span>
                <span className="diff-text">{r.text || " "}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </Modal>
  );
}
