import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, UNAUTHORIZED_EVENT } from "./api.js";
import { useHashRoute, useInterval } from "./hooks.js";
import { fmtBytes } from "./fmt.js";
import { loadChainPresets, saveChainPreset, deleteChainPreset } from "./presets.js";
import LoginView from "./components/LoginView.jsx";
import LogPicker from "./components/LogPicker.jsx";
import ScriptPicker from "./components/ScriptPicker.jsx";
import ScriptEditor from "./components/ScriptEditor.jsx";
import ParamForm from "./components/ParamForm.jsx";
import JobsPanel from "./components/JobsPanel.jsx";
import JobDetail from "./components/JobDetail.jsx";

const ACTIVE = new Set(["queued", "waiting", "running"]);
const JOB_ID_RE = /^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$/;

/** Map the URL hash to a view: "#/" → home, "#/job/<id>" → job (anything else → home). */
function parseRoute(hash) {
  const m = /^#\/job\/([0-9a-z-]+)$/i.exec(hash || "");
  if (m && JOB_ID_RE.test(m[1])) return { name: "job", id: m[1] };
  return { name: "home" };
}

function defaultParams(script) {
  const out = {};
  for (const spec of script.parameters) {
    if (spec.type === "enum") out[spec.name] = spec.default || spec.choices[0] || "";
    else if (spec.type === "multi") out[spec.name] = Array.isArray(spec.default) ? spec.default : [];
    else if (spec.type === "boolean") out[spec.name] = !!spec.default;
    else out[spec.name] = spec.default ?? "";
  }
  return out;
}

/** Convert UI string values to JSON types per the script manifest. */
function serializeParams(script, params) {
  const out = {};
  for (const spec of script.parameters) {
    const v = params[spec.name];
    if (v === "" || v == null) continue;
    if (spec.type === "integer" || spec.type === "float") out[spec.name] = Number(v);
    else if (spec.type === "multi") out[spec.name] = v;
    else out[spec.name] = v;
  }
  return out;
}

export default function App() {
  const [auth, setAuth] = useState("unknown"); // unknown | logged_out | logged_in
  const [logs, setLogs] = useState([]);
  const [scripts, setScripts] = useState([]);
  const [health, setHealth] = useState(null);
  const [selectedLog, setSelectedLog] = useState(null);
  const [steps, setSteps] = useState([]); // [{key, scriptId, params}]
  const [mode, setMode] = useState("parallel"); // parallel | chain
  const [label, setLabel] = useState("");      // optional tag for a submission
  const [chainPresets, setChainPresets] = useState(() => loadChainPresets());
  const [jobs, setJobs] = useState([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsLimit, setJobsLimit] = useState(200); // how much of the history to fetch
  const jobsLimitRef = useRef(200);
  const [jobSearch, setJobSearch] = useState("");
  const jobQueryRef = useRef(""); // what refreshJobs should ask the server for
  const [jobStatus, setJobStatus] = useState(""); // "" = all
  const jobStatusRef = useRef("");
  const view = parseRoute(useHashRoute()); // hash is the source of truth for the view
  const openJob = useCallback((id) => { window.location.hash = `#/job/${id}`; }, []);
  const goHome = useCallback(() => { window.location.hash = "#/"; }, []);
  const [toast, setToast] = useState(null);
  const [scriptEditor, setScriptEditor] = useState(null); // null | "new" | script id
  const [busy, setBusy] = useState(false);
  const [notifPerm, setNotifPerm] = useState(
    typeof Notification !== "undefined" ? Notification.permission : "unsupported"
  );

  // ---------- data loading ----------
  const showError = useCallback((e) => {
    setToast({ kind: "error", text: String(e.message || e) });
  }, []);

  const loadAll = useCallback(() => {
    api.listLogs().then((r) => setLogs(r.logs)).catch(() => {});
    api.listScripts().then((r) => setScripts(r.scripts)).catch(() => {});
    api.health().then(setHealth).catch(() => {});
  }, []);

  const refreshJobs = useCallback(() => {
    api.listJobs({ q: jobQueryRef.current, status: jobStatusRef.current, limit: jobsLimitRef.current })
      .then((r) => {
        setJobs(r.jobs);
        setJobsTotal(r.total);
      })
      .catch(() => {});
  }, []);

  const loadMoreJobs = () => {
    jobsLimitRef.current += 200;
    setJobsLimit(jobsLimitRef.current);
    refreshJobs();
  };

  // debounce the search box: one request per pause, not one per keystroke
  useEffect(() => {
    const next = jobSearch.trim();
    const t = setTimeout(() => {
      if (jobQueryRef.current === next) return;
      jobQueryRef.current = next;
      refreshJobs();
    }, 250);
    return () => clearTimeout(t);
  }, [jobSearch, refreshJobs]);

  // status chips: refetch immediately on change (no debounce needed)
  useEffect(() => {
    if (jobStatusRef.current === jobStatus) return;
    jobStatusRef.current = jobStatus;
    refreshJobs();
  }, [jobStatus, refreshJobs]);

  // ---------- authentication ----------
  useEffect(() => {
    let cancelled = false;
    api.authStatus().then(({ auth_required }) => {
      if (cancelled) return;
      if (!auth_required) {
        setAuth("logged_in");
        loadAll();
        return;
      }
      api.listLogs().then(
        () => !cancelled && setAuth("logged_in"),
        () => !cancelled && setAuth("logged_out")
      );
    }).catch(() => setAuth("logged_in") /* server unreachable; show app */);
    return () => { cancelled = true; };
  }, [loadAll]);

  useEffect(() => {
    const handler = () => setAuth("logged_out");
    window.addEventListener(UNAUTHORIZED_EVENT, handler);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler);
  }, []);

  useEffect(() => {
    if (auth === "logged_in") {
      loadAll();
      refreshJobs();
    }
  }, [auth, loadAll, refreshJobs]);

  const logout = async () => {
    await api.logout().catch(() => {});
    setAuth("logged_out");
  };

  // ---------- job polling + finish notifications ----------
  const hasActive = jobs.some((j) => ACTIVE.has(j.status));
  useInterval(refreshJobs, auth === "logged_in" && hasActive ? 1500 : 5000);

  const prevStatus = useRef({});
  useEffect(() => {
    for (const j of jobs) {
      const prev = prevStatus.current[j.id];
      if (prev && ACTIVE.has(prev) && !ACTIVE.has(j.status)) {
        const ok = j.status === "completed";
        setToast({
          kind: ok ? "ok" : "error",
          text: `${j.script_name} ${j.status}${ok ? "" : ` — ${j.error || ""}`}`,
        });
        if (typeof Notification !== "undefined" && Notification.permission === "granted") {
          try {
            new Notification(`LogSmith: job ${j.status}`, {
              body: `${j.script_name} — ${j.log_name}`,
              tag: j.id,
            });
          } catch { /* notification failed; toast still shown */ }
        }
      }
      prevStatus.current[j.id] = j.status;
    }
  }, [jobs]);

  const askNotifPermission = async () => {
    if (typeof Notification === "undefined") return;
    const perm = await Notification.requestPermission();
    setNotifPerm(perm);
  };

  // ---------- toast auto-dismiss ----------
  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => setToast(null), 4500);
    return () => clearTimeout(t);
  }, [toast]);

  // ---------- step selection (ordered; same script may appear twice) ----------
  const scriptById = useMemo(
    () => Object.fromEntries(scripts.map((s) => [s.id, s])),
    [scripts]
  );

  const stepCounts = useMemo(() => {
    const counts = {};
    for (const step of steps) counts[step.scriptId] = (counts[step.scriptId] || 0) + 1;
    return counts;
  }, [steps]);

  const addScript = (script) => {
    setSteps((list) => [
      ...list,
      { key: `${script.id}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`, scriptId: script.id, params: defaultParams(script) },
    ]);
  };

  const removeStep = (key) => {
    setSteps((list) => list.filter((st) => st.key !== key));
  };

  const moveStep = (key, dir) => {
    setSteps((list) => {
      const i = list.findIndex((st) => st.key === key);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= list.length) return list;
      const next = [...list];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  };

  const setParam = (key, name, value) => {
    setSteps((list) => list.map((st) =>
      st.key === key ? { ...st, params: { ...st.params, [name]: value } } : st
    ));
  };

  const applyParams = (key, scriptId, params) => {
    setSteps((list) => list.map((st) =>
      st.key === key ? { ...st, params: { ...defaultParams(scriptById[scriptId]), ...params } } : st
    ));
  };

  // ---------- saved chains (reusable pipelines, localStorage) ----------
  const presetNames = Object.keys(chainPresets);

  const saveChain = () => {
    if (steps.length === 0) return;
    const name = window.prompt("Save this pipeline as:", "");
    if (!name || !name.trim()) return;
    saveChainPreset(name.trim(), steps.map(({ scriptId, params }) => ({ scriptId, params })));
    setChainPresets(loadChainPresets());
    setToast({ kind: "ok", text: `Pipeline "${name.trim()}" saved — load it anytime above` });
  };

  const loadChain = (name) => {
    const preset = chainPresets[name];
    if (!preset) return;
    const loaded = preset.steps
      .map((st) => {
        const script = scriptById[st.scriptId];
        if (!script) return null;
        return {
          key: `${st.scriptId}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          scriptId: st.scriptId,
          params: { ...defaultParams(script), ...st.params },
        };
      })
      .filter(Boolean);
    const dropped = preset.steps.length - loaded.length;
    if (loaded.length === 0) {
      setToast({ kind: "error", text: "None of this pipeline's scripts exist anymore" });
      return;
    }
    setSteps(loaded);
    setMode("chain");
    if (dropped > 0) {
      setToast({ kind: "error", text: `${dropped} step(s) dropped — script(s) no longer available` });
    }
  };

  const removeChainPreset = (name) => {
    deleteChainPreset(name);
    setChainPresets(loadChainPresets());
  };

  // ---------- log upload (dropzone) ----------
  const uploadFiles = async (files) => {
    const uploaded = [];
    const failed = [];
    for (const file of files) {
      try {
        uploaded.push(await api.uploadLog(file));
      } catch (e) {
        failed.push(`${file.name}: ${e.message}`);
      }
    }
    if (uploaded.length) {
      loadAll();
      // convenient: selecting the first upload when nothing is selected yet
      if (!selectedLog && uploaded.length >= 1 && uploaded[0].log) {
        setSelectedLog(uploaded[0].log);
      }
    }
    if (failed.length) {
      setToast({ kind: "error", text: `Upload failed — ${failed.join(" · ")}` });
    } else if (uploaded.length === 1) {
      setToast({ kind: "ok", text: `Uploaded ${uploaded[0].saved} (${fmtBytes(uploaded[0].size)})` });
    } else if (uploaded.length > 1) {
      setToast({ kind: "ok", text: `Uploaded ${uploaded.length} files` });
    }
  };

  // ---------- script editor (create/edit/delete scripts in the browser) ----------
  const onScriptSaved = (script) => {
    loadAll();
    setScriptEditor(null);
    setToast({ kind: "ok", text: `Script "${script.name}" saved — it's ready to use` });
  };

  const onScriptDeleted = (scriptId, name) => {
    loadAll();
    setScriptEditor(null);
    // drop drafted steps that referenced the deleted script
    setSteps((list) => list.filter((st) => st.scriptId !== scriptId));
    setToast({ kind: "ok", text: `Script "${name}" deleted (existing job history is kept)` });
  };

  const run = async () => {
    if (!selectedLog || steps.length === 0) return;
    setBusy(true);
    try {
      const body = {
        log_path: selectedLog.path,
        mode,
        label: label.trim() || undefined,
        jobs: steps.map((st) => ({
          script_id: st.scriptId,
          params: serializeParams(scriptById[st.scriptId], st.params),
        })),
      };
      const res = await api.createJobs(body);
      const isChain = mode === "chain" && res.jobs.length > 1;
      setToast({
        kind: "ok",
        text: isChain
          ? `Chain of ${res.jobs.length} steps queued — each step feeds the next`
          : `${res.jobs.length} job${res.jobs.length > 1 ? "s" : ""} queued — watch the Jobs panel`,
      });
      refreshJobs();
    } catch (e) {
      showError(e);
    } finally {
      setBusy(false);
    }
  };

  const cancelJob = async (id) => { await api.cancelJob(id).catch(() => {}); refreshJobs(); };
  const deleteJob = async (id) => { await api.deleteJob(id).catch(() => {}); refreshJobs(); };
  const clearFinished = async () => {
    await Promise.all(
      jobs.filter((j) => !ACTIVE.has(j.status)).map((j) => api.deleteJob(j.id).catch(() => {}))
    );
    refreshJobs();
  };
  const pruneOld = async () => {
    const suggested = health?.job_retention_days || 30;
    const answer = window.prompt("Delete jobs older than how many days?", String(suggested));
    if (!answer) return;
    const days = parseInt(answer, 10);
    if (!days || days < 1) return;
    try {
      const res = await api.pruneJobs(days);
      setToast({ kind: "ok", text: `Pruned ${res.removed} job(s) older than ${days} day(s)` });
      refreshJobs();
      api.health().then(setHealth).catch(() => {});
    } catch (e) {
      showError(e);
    }
  };

  // ---------- render ----------
  if (auth !== "logged_in") {
    return auth === "logged_out" ? (
      <LoginView onSuccess={() => setAuth("logged_in")} />
    ) : (
      <div className="login-wrap"><div className="card login-card muted">Connecting…</div></div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">▤</span> LogSmith
        </div>
        <div className="muted topbar-sub">
          server-side log analysis workbench
          {health && ` · ${health.max_concurrent_jobs} parallel jobs`}
        </div>
        <div className="topbar-actions">
          {notifPerm === "default" && (
            <button className="btn ghost sm" title="Enable job-finished notifications"
                    onClick={askNotifPermission}>
              🔔 Enable alerts
            </button>
          )}
          {notifPerm === "granted" && <span className="muted sm" title="Notifications on">🔔</span>}
          {health?.auth_required && (
            <button className="btn ghost sm" onClick={logout}>Sign out</button>
          )}
        </div>
      </header>

      <main className="layout">
        {view.name === "home" ? (
          <>
            <div className="flow">
              <LogPicker
                logs={logs}
                value={selectedLog}
                onSelect={setSelectedLog}
                logsDir={health?.logs_dir || "logs/"}
                onUploadFiles={uploadFiles}
              />
              <ScriptPicker
                scripts={scripts}
                counts={stepCounts}
                onAdd={addScript}
                onNewScript={() => setScriptEditor("new")}
                onEditScript={(s) => setScriptEditor(s.id)}
              />
              {(steps.length > 0 || mode === "chain") && (
                <section className="card step">
                  <div className="step-head">
                    <span className="step-num">3</span>
                    <span className="step-title">
                      {mode === "chain" ? "Chain steps" : "Scripts & parameters"}
                    </span>
                    <span className="step-hint">
                      {mode === "chain"
                        ? "runs in order — each step's input is the previous step's output"
                        : "empty fields use script defaults"}
                    </span>
                  </div>
                  {mode === "chain" && (
                    <div className="chain-presets">
                      <span className="muted sm">Saved pipelines:</span>
                      <select
                        className="preset-select"
                        value=""
                        onChange={(e) => e.target.value && loadChain(e.target.value)}
                      >
                        <option value="">📂 Load…</option>
                        {presetNames.map((n) => (
                          <option key={n} value={n}>{n}</option>
                        ))}
                      </select>
                      {presetNames.length > 0 && (
                        <select
                          className="preset-select"
                          value=""
                          title="Delete a saved pipeline"
                          onChange={(e) => {
                            if (e.target.value &&
                                window.confirm(`Delete saved pipeline "${e.target.value}"?`)) {
                              removeChainPreset(e.target.value);
                            }
                          }}
                        >
                          <option value="">🗑 Delete…</option>
                          {presetNames.map((n) => (
                            <option key={n} value={n}>{n}</option>
                          ))}
                        </select>
                      )}
                      <button
                        className="btn ghost sm"
                        disabled={steps.length === 0}
                        onClick={saveChain}
                        title="Save the current ordered steps as a reusable pipeline"
                      >
                        💾 Save
                      </button>
                    </div>
                  )}
                  {steps.length === 0 ? (
                    <div className="empty">
                      No steps yet — click scripts above to add them, or load a
                      saved pipeline. Steps run in order; each step's input is
                      the previous step's output.
                    </div>
                  ) : steps.map((st, i) => {
                    const script = scriptById[st.scriptId];
                    if (!script) return null;
                    return (
                      <ParamForm
                        key={st.key}
                        script={script}
                        params={st.params}
                        stepNumber={mode === "chain" ? i + 1 : null}
                        chainMode={mode === "chain"}
                        canMoveUp={i > 0}
                        canMoveDown={i < steps.length - 1}
                        onMoveUp={() => moveStep(st.key, -1)}
                        onMoveDown={() => moveStep(st.key, 1)}
                        onChange={(name, value) => setParam(st.key, name, value)}
                        onApply={(params) => applyParams(st.key, st.scriptId, params)}
                        onRemove={() => removeStep(st.key)}
                      />
                    );
                  })}
                </section>
              )}
              <div className="runbar">
                <div className="runbar-left">
                  <div className="mode-toggle" role="group" aria-label="execution mode">
                    <button
                      className={mode === "parallel" ? "on" : ""}
                      onClick={() => setMode("parallel")}
                      title="All scripts run at the same time on the same log"
                    >⚡ Parallel</button>
                    <button
                      className={mode === "chain" ? "on" : ""}
                      onClick={() => setMode("chain")}
                      title="Steps run in order; each step's {log} is the previous step's output"
                    >🔗 Chain</button>
                  </div>
                  <input
                    className="label-input"
                    type="text"
                    placeholder="Label (optional)"
                    maxLength={80}
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    title="A name for this submission — searchable in the Jobs panel"
                  />
                  <div className="runbar-info">
                    {selectedLog
                      ? steps.length > 0
                        ? <><span className="mono">{selectedLog.name}</span> × {steps.length} step(s)</>
                        : "Now add one or more scripts"
                      : "Select a log file to begin"}
                  </div>
                </div>
                <button
                  className="btn primary lg"
                  disabled={!selectedLog || steps.length === 0 || busy}
                  onClick={run}
                >
                  {busy ? "Starting…" : mode === "chain" ? "Run chain ▸" : "Run ▸"}
                </button>
              </div>
            </div>

            <JobsPanel
              jobs={jobs}
              total={jobsTotal}
              onLoadMore={loadMoreJobs}
              query={jobSearch}
              onSearch={setJobSearch}
              status={jobStatus}
              onStatus={setJobStatus}
              onOpen={openJob}
              onCancel={cancelJob}
              onDelete={deleteJob}
              onClearFinished={clearFinished}
              onPrune={pruneOld}
              dataSize={health?.data_dir_size}
            />
          </>
        ) : (
          <JobDetail
            id={view.id}
            onBack={() => { goHome(); refreshJobs(); }}
            onOpenJob={openJob}
            onRerun={openJob}
            onChanged={refreshJobs}
          />
        )}
      </main>

      {scriptEditor && (
        <ScriptEditor
          scriptId={scriptEditor === "new" ? null : scriptEditor}
          onClose={() => setScriptEditor(null)}
          onSaved={onScriptSaved}
          onDeleted={onScriptDeleted}
          onError={showError}
        />
      )}
      {toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}
    </div>
  );
}
