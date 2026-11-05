import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, UNAUTHORIZED_EVENT } from "./api.js";
import { useHashRoute, useInterval } from "./hooks.js";
import { fmtBytes } from "./fmt.js";
import { migrateLegacyChains } from "./presets.js";
import { defaultParams, serializeParams, selectionNeedsLog } from "./params.js";
import LoginView from "./components/LoginView.jsx";
import LogPicker from "./components/LogPicker.jsx";
import ScriptPicker from "./components/ScriptPicker.jsx";
import ScriptEditor from "./components/ScriptEditor.jsx";
import ParamForm from "./components/ParamForm.jsx";
import JobsPanel from "./components/JobsPanel.jsx";
import SchedulesPanel from "./components/SchedulesPanel.jsx";
import JobDetail from "./components/JobDetail.jsx";
import TerminalPanel from "./components/TerminalPanel.jsx";
import UsersPanel from "./components/UsersPanel.jsx";

const ACTIVE = new Set(["queued", "waiting", "running"]);
const JOB_ID_RE = /^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$/;

/** Map the URL hash to a view: "#/" → home, "#/job/<id>" → job (anything else → home). */
function parseRoute(hash) {
  const m = /^#\/job\/([0-9a-z-]+)$/i.exec(hash || "");
  if (m && JOB_ID_RE.test(m[1])) return { name: "job", id: m[1] };
  if (hash === "#/terminal") return { name: "terminal" };
  if (hash === "#/users") return { name: "users" };
  return { name: "home" };
}

export default function App() {
  const [auth, setAuth] = useState("unknown"); // unknown | logged_out | logged_in
  const [logs, setLogs] = useState([]);
  const [scripts, setScripts] = useState([]);
  const [health, setHealth] = useState(null);
  const [selectedLog, setSelectedLog] = useState(null);
  const [fanout, setFanout] = useState([]); // extra logs the submission also runs on
  const [steps, setSteps] = useState([]); // [{key, scriptId, params}]
  const [collapsedSteps, setCollapsedSteps] = useState(() => new Set()); // step keys folded to their title bar
  const [mode, setMode] = useState("parallel"); // parallel | chain
  const [label, setLabel] = useState("");      // optional tag for a submission
  const [pipelines, setPipelines] = useState([]); // server-side saved chains, shared with the python UI
  const [schedules, setSchedules] = useState([]); // cron-fired pipelines, shared with the python UI
  const [jobs, setJobs] = useState([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsLimit, setJobsLimit] = useState(200); // how much of the history to fetch
  const jobsLimitRef = useRef(200);
  const [jobSearch, setJobSearch] = useState("");
  const jobQueryRef = useRef(""); // what refreshJobs should ask the server for
  const [jobStatus, setJobStatus] = useState(""); // "" = all
  const jobStatusRef = useRef("");
  // admins oversee everyone's runs; this narrows the list to one account
  const [jobOwner, setJobOwner] = useState("");
  const jobOwnerRef = useRef("");
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

  const loadScripts = useCallback(
    () => api.listScripts().then((r) => setScripts(r.scripts)).catch(() => {}),
    []
  );
  const loadAll = useCallback(() => {
    api.listLogs().then((r) => setLogs(r.logs)).catch(() => {});
    loadScripts();
    api.health().then(setHealth).catch(() => {});
  }, [loadScripts]);

  // sharing is a move between the private space and the shared library —
  // the script keeps its id, so jobs, pipelines and schedules keep working
  const shareScript = useCallback(async (s) => {
    try {
      await api.shareScript(s.id);
      setToast({ kind: "ok", text: `Shared ${s.id} — every user can now run it` });
      loadScripts();
    } catch (e) { showError(e); }
  }, [loadScripts, showError]);
  const unshareScript = useCallback(async (s) => {
    try {
      await api.unshareScript(s.id);
      setToast({ kind: "ok", text: `Unshared ${s.id} — it moved to your private scripts` });
      loadScripts();
    } catch (e) { showError(e); }
  }, [loadScripts, showError]);

  const loadPipelines = useCallback(
    () => api.listPipelines().then((r) => setPipelines(r.pipelines || [])).catch(() => {}),
    []
  );

  const loadSchedules = useCallback(
    () => api.listSchedules().then((r) => setSchedules(r.schedules || [])).catch(() => {}),
    []
  );

  const refreshJobs = useCallback(() => {
    api.listJobs({ q: jobQueryRef.current, status: jobStatusRef.current,
                    user: jobOwnerRef.current, limit: jobsLimitRef.current })
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

  // the admin's owner filter, debounced like the search box
  useEffect(() => {
    const next = jobOwner.trim();
    const t = setTimeout(() => {
      if (jobOwnerRef.current === next) return;
      jobOwnerRef.current = next;
      refreshJobs();
    }, 250);
    return () => clearTimeout(t);
  }, [jobOwner, refreshJobs]);

  // ---------- authentication ----------
  const [multiUser, setMultiUser] = useState(false);
  const [authUser, setAuthUser] = useState(null); // {username, role} in multi-user mode
  const refreshIdentity = useCallback(() => {
    // re-probe after login or after the Users panel bootstraps the first
    // account (the server mints that session, flipping multi-user on)
    api.authStatus().then((s) => {
      setMultiUser(!!s.multi_user);
      setAuthUser(s.user || null);
    }).catch(() => {});
    api.health().then(setHealth).catch(() => {});
  }, []);
  useEffect(() => {
    let cancelled = false;
    api.authStatus().then(({ auth_required, multi_user, user }) => {
      if (cancelled) return;
      setMultiUser(!!multi_user);
      setAuthUser(user || null);
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
      loadPipelines();
      loadSchedules();
      refreshJobs();
    }
  }, [auth, loadAll, loadPipelines, loadSchedules, refreshJobs]);

  // one-time per page load: chains saved by the old localStorage UI move
  // to the server, so every browser and the python UI see them. A failed
  // move keeps the local copy — the next load tries again.
  const migratedRef = useRef(false);
  useEffect(() => {
    if (auth !== "logged_in" || migratedRef.current) return;
    migratedRef.current = true;
    migrateLegacyChains({ listPipelines: api.listPipelines, savePipeline: api.savePipeline })
      .then((moved) => { if (moved > 0) loadPipelines(); })
      .catch(() => {});
  }, [auth, loadPipelines]);

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
    setCollapsedSteps((set) => { const next = new Set(set); next.delete(key); return next; });
  };

  const toggleCollapsed = (key) => {
    setCollapsedSteps((set) => {
      const next = new Set(set);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
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

  // ---------- saved pipelines (server-side chains, shared with the python UI) ----------
  const presetNames = pipelines.map((p) => p.name);
  const pipelineByName = useMemo(
    () => Object.fromEntries(pipelines.map((p) => [p.name, p])),
    [pipelines]
  );

  const saveChain = async () => {
    if (steps.length === 0) return;
    const name = window.prompt("Save this pipeline as:", "");
    if (!name || !name.trim()) return;
    try {
      await api.savePipeline({
        name: name.trim(),
        steps: steps.map(({ scriptId, params }) => ({ script_id: scriptId, params })),
      });
      await loadPipelines();
      setToast({ kind: "ok", text: `Pipeline "${name.trim()}" saved — load it anytime above` });
    } catch (e) {
      showError(e);
    }
  };

  const loadChain = (name) => {
    const preset = pipelineByName[name];
    if (!preset) return;
    const loaded = preset.steps
      .map((st) => {
        const script = scriptById[st.script_id];
        if (!script) return null;
        return {
          key: `${st.script_id}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          scriptId: st.script_id,
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
    // A loaded pipeline is pre-configured — load it to run, not to re-edit:
    // the stack starts collapsed to title bars.
    setCollapsedSteps(new Set(loaded.map((st) => st.key)));
    if (dropped > 0) {
      setToast({ kind: "error", text: `${dropped} step(s) dropped — script(s) no longer available` });
    }
  };

  const removeChainPreset = async (name) => {
    try {
      await api.deletePipeline(name);
      setPipelines((list) => list.filter((p) => p.name !== name));
    } catch (e) {
      showError(e);
    }
  };

  // ---------- schedules (cron-fired pipelines) ----------

  const saveSchedule = async (body) => {
    try {
      await api.saveSchedule(body);
      await loadSchedules();
      setToast({ kind: "ok", text: `Schedule "${body.name}" saved — it fires on the server's clock` });
    } catch (e) {
      showError(e);
    }
  };

  const removeSchedule = async (name) => {
    try {
      await api.deleteSchedule(name);
      setSchedules((list) => list.filter((s) => s.name !== name));
    } catch (e) {
      showError(e);
    }
  };

  const toggleSchedule = async (name) => {
    try {
      const r = await api.toggleSchedule(name);
      setSchedules((list) => list.map((s) => (s.name === name ? r.schedule : s)));
      setToast({ kind: "ok", text: `Schedule "${name}" ${r.schedule.enabled ? "resumed" : "paused"}` });
    } catch (e) {
      showError(e);
    }
  };

  const runScheduleNow = async (name) => {
    try {
      const r = await api.runSchedule(name);
      loadSchedules();
      refreshJobs();
      // "skipped: previous run still active" and fire errors come back as a
      // result, not an exception — toast them honestly
      const failed = String(r.result || "").startsWith("error");
      setToast({ kind: failed ? "error" : "ok", text: `${name}: ${r.result}` });
    } catch (e) {
      showError(e);
    }
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

  // test-run from the editor: the script was saved, a job was just submitted
  const onScriptRan = (jobId) => {
    loadAll();
    refreshJobs();
    setScriptEditor(null);
    openJob(jobId);
  };

  // Import a tar.gz (the export format, or a pack of flat scripts). An id
  // that already exists is a 409 — offer to replace, then retry once.
  const importScriptFile = async (file) => {
    const run = (overwrite) => api.importScripts(file, { overwrite });
    let result;
    try {
      result = await run(false);
    } catch (e) {
      if (String(e.message).startsWith("409") &&
          window.confirm(`${e.message}\n\nReplace the existing script(s)?`)) {
        try {
          result = await run(true);
        } catch (e2) {
          showError(e2);
          return;
        }
      } else {
        showError(e);
        return;
      }
    }
    loadAll();
    setToast({
      kind: "ok",
      text: `Imported ${result.imported.length} script(s): ${result.imported.join(", ")}`,
    });
  };

  const toggleFanout = (log) => {
    setFanout((list) =>
      list.some((l) => l.path === log.path)
        ? list.filter((l) => l.path !== log.path)
        : [...list, log]);
  };

  // Whether this selection needs a log at all: in chain mode only the
  // first step reads the log (the rest read the previous step's output);
  // in parallel every step does. Log-free scripts run without one.
  const logRequired = selectionNeedsLog(steps, scriptById, mode);

  const run = async () => {
    // Without a log, one log-free submission is made (the server rejects
    // scripts that read {log}); with one, the fan-out runs per log.
    if (steps.length === 0 || (!selectedLog && logRequired)) return;
    setBusy(true);
    try {
      // fan-out: the same submission runs once per selected log
      const targets = selectedLog
        ? [selectedLog, ...fanout.filter((l) => l.path !== selectedLog.path)]
        : [];
      const jobsSpec = steps.map((st) => ({
        script_id: st.scriptId,
        params: serializeParams(scriptById[st.scriptId], st.params),
      }));
      let queued = 0;
      if (targets.length === 0) {
        const res = await api.createJobs({
          log_path: "",
          mode,
          label: label.trim() || undefined,
          jobs: jobsSpec,
        });
        queued += res.jobs.length;
      } else {
        for (const target of targets) {
          const res = await api.createJobs({
            log_path: target.path,
            mode,
            label: label.trim() || undefined,
            jobs: jobsSpec,
          });
          queued += res.jobs.length;
        }
      }
      const isChain = mode === "chain" && steps.length > 1;
      setToast({
        kind: "ok",
        text: targets.length > 1
          ? `${queued} job${queued > 1 ? "s" : ""} queued across ${targets.length} logs — watch the Jobs panel`
          : targets.length === 1
            ? isChain
              ? `Chain of ${queued} steps queued — each step feeds the next`
              : `${queued} job${queued > 1 ? "s" : ""} queued — watch the Jobs panel`
            : isChain
              ? `Chain of ${queued} steps queued without a log`
              : `${queued} log-free job${queued > 1 ? "s" : ""} queued — watch the Jobs panel`,
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
      const cache = res.cache_removed
        ? ` · freed ${res.cache_removed} decompressed cache file(s)` : "";
      setToast({ kind: "ok", text: `Pruned ${res.removed} job(s) older than ${days} day(s)${cache}` });
      refreshJobs();
      api.health().then(setHealth).catch(() => {});
    } catch (e) {
      showError(e);
    }
  };

  // ---------- render ----------
  // who may open #/users: an admin account, or the local operator while
  // the server still has no accounts (that's the bootstrap path)
  const isAdmin = authUser?.role === "admin" || (health && !health.multi_user);
  if (auth !== "logged_in") {
    return auth === "logged_out" ? (
      <LoginView multiUser={multiUser} onSuccess={() => {
        setAuth("logged_in");
        refreshIdentity();
      }} />
    ) : (
      <div className="login-wrap"><div className="card login-card muted">Connecting…</div></div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/" title="Back to the workbench">
          <span className="brand-mark">▤</span> LogSmith
        </a>
        <div className="muted topbar-sub">
          server-side log analysis workbench
          {health && ` · ${health.max_concurrent_jobs} parallel jobs`}
          {health?.docker_sandbox && ` · 🐳 sandboxed`}
        </div>
        <div className="topbar-actions">
          {isAdmin && (
            <a className={`btn ghost sm ${view.name === "users" ? "on" : ""}`}
               href="#/users" title="Manage user accounts">
              👤 Users
            </a>
          )}
          {health?.terminal && isAdmin && (
            <a className={`btn ghost sm ${view.name === "terminal" ? "on" : ""}`}
               href="#/terminal" title="Interactive shell on the server (LOGSMITH_TERMINAL)">
              ⌨ Terminal
            </a>
          )}
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
                fanout={fanout}
                onToggleFanout={toggleFanout}
                logsDir={health?.logs_dir || "logs/"}
                onUploadFiles={uploadFiles}
              />
              <ScriptPicker
                scripts={scripts}
                counts={stepCounts}
                onAdd={addScript}
                onNewScript={() => setScriptEditor("new")}
                onEditScript={(s) => setScriptEditor(s.id)}
                canEditScripts={health?.script_editing !== false}
                onImport={importScriptFile}
                me={authUser}
                onShare={shareScript}
                onUnshare={unshareScript}
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
                    {steps.length > 1 && (
                      <span className="head-action">
                        <button
                          className="btn ghost sm"
                          title="Fold every step to its title bar — reorder controls stay put"
                          onClick={() => setCollapsedSteps(new Set(steps.map((st) => st.key)))}
                        >⇈ Collapse all</button>
                        <button
                          className="btn ghost sm"
                          title="Open every step's parameter form"
                          onClick={() => setCollapsedSteps(new Set())}
                        >⇊ Expand all</button>
                      </span>
                    )}
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
                        collapsed={collapsedSteps.has(st.key)}
                        onToggleCollapsed={() => toggleCollapsed(st.key)}
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
                        ? fanout.length > 0
                          ? <>{fanout.length + 1} logs × {steps.length} step(s) = {(fanout.length + 1) * steps.length} job(s)</>
                          : <><span className="mono">{selectedLog.name}</span> × {steps.length} step(s)</>
                        : "Now add one or more scripts"
                      : steps.length > 0
                        ? logRequired
                          ? "Select a log file — the first step reads it"
                          : "No log — log-free run"
                        : "Select a log file to begin"}
                  </div>
                </div>
                <button
                  className="btn primary lg"
                  disabled={steps.length === 0 || (!selectedLog && logRequired) || busy}
                  onClick={run}
                  title={!selectedLog && logRequired
                    ? "The first step reads its input from {log} — select a log"
                    : undefined}
                >
                  {busy ? "Starting…" : mode === "chain" ? "Run chain ▸" : "Run ▸"}
                </button>
              </div>

              <SchedulesPanel
                schedules={schedules}
                pipelines={pipelines}
                scripts={scripts}
                logs={logs}
                defaultLog={selectedLog && !selectedLog.custom ? selectedLog.name : ""}
                defaultLogPath={selectedLog?.custom ? selectedLog.path : ""}
                onSave={saveSchedule}
                onToggle={toggleSchedule}
                onRun={runScheduleNow}
                onDelete={removeSchedule}
              />
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
              onPrune={isAdmin ? pruneOld : undefined}
              dataSize={health?.data_dir_size}
              me={authUser}
              ownerFilter={jobOwner}
              onOwnerFilter={setJobOwner}
            />
          </>
        ) : view.name === "terminal" ? (
          isAdmin ? (
            <TerminalPanel onBack={() => { goHome(); refreshJobs(); }} />
          ) : multiUser && authUser ? (
            // the server enforces it; this just avoids mounting a panel
            // that could only fail — the shell is the admin's
            <div className="card">
              <h2>Terminal</h2>
              <p className="muted">
                The terminal is an admin feature — a shell on the server is
                not part of a regular account's workbench.
              </p>
              <button className="btn" onClick={goHome}>← Back</button>
            </div>
          ) : null // still resolving who is asking — no flash of a denial
        ) : view.name === "users" ? (
          <UsersPanel
            me={authUser}
            multiUser={multiUser}
            onIdentityChange={refreshIdentity}
            onBack={goHome}
          />
        ) : (
          <JobDetail
            id={view.id}
            onBack={() => { goHome(); refreshJobs(); }}
            onOpenJob={openJob}
            onRerun={openJob}
            onChanged={refreshJobs}
            me={authUser}
            multiUser={multiUser}
          />
        )}
      </main>

      {scriptEditor && (
        <ScriptEditor
          scriptId={scriptEditor === "new" ? null : scriptEditor}
          onClose={() => setScriptEditor(null)}
          onSaved={onScriptSaved}
          onDeleted={onScriptDeleted}
          onRan={onScriptRan}
          onError={showError}
          me={authUser}
          multiUser={multiUser}
        />
      )}
      {toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}
    </div>
  );
}
