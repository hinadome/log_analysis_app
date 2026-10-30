import { useCallback, useEffect, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { api } from "../api.js";
import { useInterval } from "../hooks.js";
import { cx, elapsed, fmtBytes, fmtDate } from "../fmt.js";
import { OutputView, StatusBadge } from "./ui.jsx";

const ACTIVE = new Set(["queued", "waiting", "running"]);
const RUNNING = new Set(["queued", "running"]);
const TEXTY = /(text|json|markdown|csv)/i;
const isMd = (name) => /\.(md|markdown)$/i.test(name);

const STEP_ICON = {
  completed: "✓",
  running: "●",
  queued: "◔",
  waiting: "○",
  skipped: "⊘",
  failed: "✕",
  cancelled: "—",
};

function shellQuote(token) {
  return /^[A-Za-z0-9_@%+=:,./-]*$/.test(token) ? token : `'${token.replace(/'/g, "'\\''")}'`;
}

export default function JobDetail({ id, onBack, onOpenJob, onRerun, onChanged }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const [liveText, setLiveText] = useState("");   // stdout accumulated via SSE
  const [stderrTail, setStderrTail] = useState("");
  const [fullOut, setFullOut] = useState(undefined); // undefined = not loaded
  const [sseFailed, setSseFailed] = useState(false);
  const [tab, setTab] = useState("result");
  const [fileViews, setFileViews] = useState({});
  const [fileModes, setFileModes] = useState({}); // name -> "rendered" | "raw"
  const [copied, setCopied] = useState(false);
  const tailRef = useRef(null);
  const stickBottom = useRef(true);

  const active = job && ACTIVE.has(job.status);
  const running = job && RUNNING.has(job.status);

  const load = useCallback(() => {
    api.getJob(id).then(setJob).catch((e) => setError(String(e.message || e)));
  }, [id]);

  useEffect(() => {
    setJob(null);
    setError(null);
    setFullOut(undefined);
    setLiveText("");
    setStderrTail("");
    setSseFailed(false);
    setTab("result");
    setFileViews({});
    setFileModes({});
    load();
  }, [id, load]);

  // Live updates via server-sent events (falls back to polling on error).
  useEffect(() => {
    if (sseFailed) return undefined;
    const source = new EventSource(api.jobEventsUrl(id));
    let closed = false;

    source.onmessage = (e) => {
      let data;
      try {
        data = JSON.parse(e.data);
      } catch {
        return;
      }
      if (data.type === "stdout") {
        setLiveText((t) => t + data.text);
      } else if (data.type === "status") {
        setStderrTail(data.stderr_tail ?? "");
        setJob((j) => (j ? {
          ...j,
          status: data.status,
          error: data.error,
          exit_code: data.exit_code,
          started_at: data.started_at,
          finished_at: data.finished_at,
          stdout_size: data.stdout_size,
          stderr_size: data.stderr_size,
          output_files: data.output_files,
        } : j));
        if (!ACTIVE.has(data.status)) {
          closed = true;
          source.close();
          api.getJobStdout(id).then(setFullOut).catch(() => setFullOut(""));
          api.getJob(id).then(setJob).catch(() => {});
        }
      } else if (data.type === "gone") {
        closed = true;
        source.close();
      }
    };
    source.onerror = () => {
      if (!closed) {
        source.close();
        setSseFailed(true); // switch to polling
      }
    };
    return () => source.close();
  }, [id, sseFailed]);

  // Polling fallback when EventSource is unavailable.
  useInterval(load, sseFailed && active ? 1500 : null);

  // Load the complete stdout once the job has finished (SSE path already did).
  useEffect(() => {
    if (job && !active && fullOut === undefined && sseFailed) {
      api.getJobStdout(id).then(setFullOut).catch(() => setFullOut(""));
    }
  }, [job, active, fullOut, sseFailed, id]);

  // Keep the live tail scrolled to the bottom while running.
  useEffect(() => {
    const el = tailRef.current;
    if (el && stickBottom.current) el.scrollTop = el.scrollHeight;
  }, [liveText]);

  if (error) {
    return (
      <div className="card">
        <div className="error-banner">{error}</div>
        <button className="btn" onClick={onBack}>← Back</button>
      </div>
    );
  }
  if (!job) return <div className="card muted">Loading job…</div>;

  const liveStdout = sseFailed ? job.stdout_tail : liveText;
  const stdout = active ? (liveStdout || "(waiting for output…)") : (fullOut ?? liveStdout);
  const stderrText = stderrTail || job.stderr_tail;

  const cancel = async () => { await api.cancelJob(id).catch(() => {}); load(); onChanged(); };
  const rerun = async () => {
    const res = await api.rerunJob(id);
    onChanged();
    onRerun(res.job.id);
  };
  const rerunFromHere = async () => {
    const res = await api.rerunJob(id, job.chain_index + 1);
    onChanged();
    onRerun(res.job.id);
  };
  const del = async () => { await api.deleteJob(id).catch(() => {}); onChanged(); onBack(); };

  const viewFile = async (name) => {
    if (fileViews[name] !== undefined) {
      const { [name]: _drop, ...rest } = fileViews;
      setFileViews(rest);
      return;
    }
    const res = await fetch(api.jobFileUrl(id, name));
    const ctype = res.headers.get("content-type") || "";
    const text = TEXTY.test(ctype) ? await res.text() : "(binary file — use Download)";
    setFileViews((v) => ({ ...v, [name]: text }));
    if (isMd(name)) setFileModes((m) => ({ ...m, [name]: "rendered" }));
  };

  const copyCommand = async () => {
    const text = `$ ${job.command.map(shellQuote).join(" ")}`;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard unavailable */ }
  };

  return (
    <div className="job-detail">
      <div className="card">
        <div className="job-head">
          <button className="btn ghost sm" onClick={onBack}>← Jobs</button>
          <div className="job-head-main">
            <StatusBadge status={job.status} />
            <span className="job-title">{job.script_name}</span>
            {job.label && <span className="chip accent" title="Submission label">{job.label}</span>}
            <span className="mono muted">{job.log_name}</span>
          </div>
          <div className="job-actions">
            {active && <button className="btn danger sm" onClick={cancel}>Cancel</button>}
            {job.chain_id && job.chain_index > 0 && (
              <button
                className="btn sm"
                onClick={rerunFromHere}
                title="Re-run only this step and the ones after it, reusing earlier steps' saved outputs"
              >
                Re-run from here
              </button>
            )}
            <button className="btn sm" onClick={rerun} title="Re-run the whole chain from step 1">Re-run</button>
            <button className="btn ghost sm" onClick={del}>Delete</button>
          </div>
        </div>

        <div className="meta-grid">
          <Meta label="Job ID" value={<span className="mono sm">{job.id}</span>} />
          <Meta label="Created" value={fmtDate(job.created_at)} />
          <Meta label="Started" value={fmtDate(job.started_at)} />
          <Meta label="Duration" value={elapsed(job.started_at || job.created_at, job.finished_at) ?? "—"} />
          <Meta label="Exit code" value={job.exit_code ?? "—"} />
          <Meta label="Output size" value={fmtBytes(job.stdout_size)} />
        </div>

        {job.error && <div className="error-banner">{job.error}</div>}
        {job.status === "waiting" && (
          <div className="running-banner">
            <span className="spin">◐</span> Waiting for the previous step to finish —
            this step starts automatically with its output.
          </div>
        )}
        {running && (
          <div className="running-banner">
            <span className="spin">◐</span> Running — output below streams live from the script.
          </div>
        )}
      </div>

      {job.chain_steps && job.chain_steps.length > 0 && (
        <div className="chain-strip">
          {job.chain_steps.map((s) => (
            <button
              key={s.id}
              className={`chain-step ${s.status} ${s.id === job.id ? "current" : ""}`}
              onClick={() => s.id !== job.id && onOpenJob(s.id)}
              title={`Step ${s.index + 1}: ${s.script_name} (${s.status})`}
            >
              <span className="chain-step-idx">{s.index + 1}</span>
              <span className="chain-step-name">{s.script_name}</span>
              <span className="chain-step-icon">{STEP_ICON[s.status] || "·"}</span>
            </button>
          ))}
        </div>
      )}

      <div className="tabs">
        <Tab name="result" tab={tab} setTab={setTab}>Result</Tab>
        <Tab name="files" tab={tab} setTab={setTab}>
          Files {job.output_files.length > 0 && <span className="chip">{job.output_files.length}</span>}
        </Tab>
        <Tab name="stderr" tab={tab} setTab={setTab}>
          stderr {job.stderr_size > 0 && <span className="chip warn">{fmtBytes(job.stderr_size)}</span>}
        </Tab>
        <Tab name="details" tab={tab} setTab={setTab}>Command</Tab>
      </div>

      {tab === "result" && (
        job.status === "waiting" ? (
          <div className="card empty">
            This step has not started yet — it runs automatically when the previous
            step finishes, using that step's output as its input.
          </div>
        ) : active ? (
          <pre
            ref={tailRef}
            className="output live"
            onScroll={(e) => {
              const el = e.target;
              stickBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            }}
          >{stdout}</pre>
        ) : (
          <OutputView text={fullOut ?? liveStdout ?? "Loading…"} />
        )
      )}

      {tab === "files" && (
        <div className="card">
          {job.output_files.length === 0 && (
            <div className="empty">This job produced no artifact files.</div>
          )}
          {job.output_files.map((name) => (
            <div key={name} className="file-row">
              <span className="mono file-name">{name}</span>
              {isMd(name) && fileViews[name] !== undefined && (
                <button
                  className="btn ghost sm"
                  onClick={() => setFileModes((m) => ({
                    ...m, [name]: m[name] === "raw" ? "rendered" : "raw",
                  }))}
                >
                  {fileModes[name] === "raw" ? "Rendered" : "Raw"}
                </button>
              )}
              <button className="btn ghost sm" onClick={() => viewFile(name)}>
                {fileViews[name] !== undefined ? "Hide" : "View"}
              </button>
              <a className="btn sm" href={api.jobFileUrl(id, name)} download={name}>Download</a>
              {fileViews[name] !== undefined && (
                isMd(name) && fileModes[name] === "rendered"
                  ? (
                    <div
                      className="markdown-body file-view"
                      dangerouslySetInnerHTML={{
                        __html: DOMPurify.sanitize(marked.parse(fileViews[name])),
                      }}
                    />
                  ) : <pre className="output file-view">{fileViews[name]}</pre>
              )}
            </div>
          ))}
        </div>
      )}

      {tab === "stderr" && (
        <pre className={cx("output", job.stderr_size > 0 && "has-err")}>
          {stderrText || "(stderr is empty)"}
        </pre>
      )}

      {tab === "details" && (
        <div className="card">
          <div className="cmd-head">
            <div className="muted sm">Executed command (argv, no shell):</div>
            <button className="btn sm" onClick={copyCommand}>
              {copied ? "Copied ✓" : "Copy command"}
            </button>
          </div>
          <pre className="output cmd-view">
            <span className="tok">$ </span>
            {job.command.map((tok, i) => (
              <span key={i} className={cx("tok", { arg: i > 0 })}>{shellQuote(tok)} </span>
            ))}
          </pre>
          <div className="muted sm">Parameters:</div>
          <pre className="output cmd-view">{JSON.stringify(job.params, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

function Meta({ label, value }) {
  return (
    <div className="meta-item">
      <div className="meta-label">{label}</div>
      <div className="meta-value">{value}</div>
    </div>
  );
}

function Tab({ name, tab, setTab, children }) {
  return (
    <button className={cx("tab", { active: tab === name })} onClick={() => setTab(name)}>
      {children}
    </button>
  );
}
