import { useCallback, useEffect, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { api } from "../api.js";
import { useInterval } from "../hooks.js";
import { cx, elapsed, fmtBytes, fmtDate } from "../fmt.js";
import { OutputView, StatusBadge } from "./ui.jsx";
import DiffView from "./DiffView.jsx";

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

const INPUT_SOURCE_LABEL = {
  log: "the selected log file",
  prev_output: "the previous step's output file",
  prev_stdout: "the previous step's stdout",
};

function shellQuote(token) {
  return /^[A-Za-z0-9_@%+=:,./-]*$/.test(token) ? token : `'${token.replace(/'/g, "'\\''")}'`;
}

export default function JobDetail({ id, onBack, onOpenJob, onRerun, onChanged, me, multiUser }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const [liveText, setLiveText] = useState("");   // stdout accumulated via SSE
  const [stderrTail, setStderrTail] = useState("");
  const [fullOut, setFullOut] = useState(undefined); // undefined = not loaded
  const [sseFailed, setSseFailed] = useState(false);
  const [tab, setTab] = useState("result");
  const [fileViews, setFileViews] = useState({});
  const [fileModes, setFileModes] = useState({}); // name -> "rendered" | "raw"
  const [inputText, setInputText] = useState(undefined); // undefined = not loaded, null = unavailable
  const [copied, setCopied] = useState(false);
  const [compare, setCompare] = useState(false);
  const tailRef = useRef(null);
  const stickBottom = useRef(true);

  const active = job && ACTIVE.has(job.status);
  const running = job && RUNNING.has(job.status);
  // a regular user manages their own runs; the shared system history is the
  // admin's. me is null for the single operator, who may do anything.
  const mayManage = Boolean(job) && (!me || me.role === "admin" || job.owner === me.username);

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
    setInputText(undefined);
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

  // The Input tab loads lazily, once per job (waiting steps have no input yet).
  // A log-free job never had one — no fetch, its own message.
  const noInput = job && !job.log_path && !job.input_path;
  useEffect(() => {
    if (tab !== "input" || !job || job.status === "waiting" || noInput || inputText !== undefined) {
      return undefined;
    }
    let cancelled = false;
    api.getJobInput(id).then((t) => !cancelled && setInputText(t));
    return () => { cancelled = true; };
  }, [tab, id, job, inputText, noInput]);

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
            <span className="mono muted">{job.log_name || "— no log"}</span>
          </div>
          <div className="job-actions">
            {/* a regular user may stop or remove their own runs; the shared
                system history (scheduler, pre-accounts) is the admin's */}
            {active && mayManage && <button className="btn danger sm" onClick={cancel}>Cancel</button>}
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
            {!active && (
              <button
                className="btn ghost sm"
                onClick={() => setCompare(true)}
                title="Diff this job's output against another completed job"
              >
                ⇄ Compare…
              </button>
            )}
            {mayManage && <button className="btn ghost sm" onClick={del}>Delete</button>}
          </div>
        </div>

        <div className="meta-grid">
          <Meta label="Job ID" value={<span className="mono sm">{job.id}</span>} />
          {multiUser && (
            <Meta
              label="Owner"
              value={job.owner
                ? (me && job.owner === me.username ? `${job.owner} (you)` : job.owner)
                : "system"}
            />
          )}
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
        <Tab name="input" tab={tab} setTab={setTab} title="What this script received as its input">Input</Tab>
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

      {tab === "input" && (
        noInput ? (
          <div className="card empty">
            No input — this script's command has no {"{log}"}, so it ran without a log file.
          </div>
        ) : job.status === "waiting" ? (
          <div className="card empty">
            This step has not started yet — its input will be the previous step's
            output, available once that step finishes.
          </div>
        ) : inputText === undefined ? (
          <div className="card empty">Loading input…</div>
        ) : inputText === null ? (
          <div className="card empty">
            The input file is not available — the source log may have been deleted,
            or this step's input was never materialized.
          </div>
        ) : (
          <div className="card">
            <div className="muted sm input-src">
              What this script received as input —{" "}
              {INPUT_SOURCE_LABEL[job.input_source] || "the selected log file"}:
            </div>
            <OutputView text={inputText} />
          </div>
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

      {compare && <DiffView jobId={id} onClose={() => setCompare(false)} />}
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

function Tab({ name, tab, setTab, title, children }) {
  return (
    <button className={cx("tab", { active: tab === name })} onClick={() => setTab(name)} title={title}>
      {children}
    </button>
  );
}
