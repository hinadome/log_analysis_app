import { useState } from "react";
import { cx, fmtDate, fmtTime } from "../fmt.js";
import { defaultParams, serializeParams } from "../params.js";
import ParamForm from "./ParamForm.jsx";

// Cron presets the form offers as one-click starting points — they just
// fill the expression field, everything stays editable.
const PRESETS = [
  { label: "every 5 min", cron: "*/5 * * * *" },
  { label: "hourly", cron: "0 * * * *" },
  { label: "daily 08:00", cron: "0 8 * * *" },
  { label: "weekdays 09:00", cron: "0 9 * * mon-fri" },
];

/**
 * Saved schedules: a pipeline or a single script, plus a log and a cron
 * expression, fired by the server's ticker. Scheduled runs are ordinary
 * jobs (labelled with the schedule's name) — they show up in the Jobs
 * panel like any other run.
 */
export default function SchedulesPanel({
  schedules, pipelines, logs, scripts, defaultLog, defaultLogPath,
  onSave, onToggle, onRun, onDelete,
}) {
  const [open, setOpen] = useState(false); // the create form starts folded
  const [form, setForm] = useState(null);  // null = not editing yet
  const set = (field, value) => setForm((f) => ({ ...f, [field]: value }));

  const scriptById = Object.fromEntries(scripts.map((s) => [s.id, s]));
  const formScript = form?.what === "script" ? scriptById[form.script] : null;

  const startNew = () => {
    setOpen(true);
    // pipelines first when one exists; otherwise straight to a script
    setForm({
      name: "", cron: "", label: "",
      what: pipelines.length ? "pipeline" : "script",
      pipeline: pipelines[0]?.name || "",
      script: "",
      params: {},
      // a custom-path selection prefill the by-path box instead
      log: defaultLogPath ? "" : (defaultLog || logs[0]?.name || ""),
      logPath: defaultLogPath || "",
      enabled: true,
    });
  };

  // Picking a script resets its parameters to the manifest defaults —
  // each script carries its own.
  const pickScript = (id) => {
    const script = scriptById[id];
    setForm((f) => ({
      ...f,
      script: id,
      params: script ? defaultParams(script) : {},
    }));
  };

  const setParam = (name, value) => {
    setForm((f) => ({ ...f, params: { ...f.params, [name]: value } }));
  };

  const applyParams = (params) => {
    setForm((f) => ({
      ...f,
      params: { ...defaultParams(formScript), ...params },
    }));
  };

  const submit = (e) => {
    e.preventDefault();
    if (!form.name.trim() || !form.cron.trim()) return;
    const base = {
      name: form.name.trim(), cron: form.cron.trim(), label: form.label.trim() || null,
      // the by-path box overrides the select while filled
      log: form.logPath.trim() || form.log, enabled: form.enabled !== false,
    };
    // A schedule is either a saved pipeline (resolved at every fire) or
    // its own single script with the parameters configured here.
    onSave(form.what === "script"
      ? { ...base, steps: [{ script_id: form.script, params: serializeParams(formScript, form.params) }] }
      : { ...base, pipeline: form.pipeline });
    setForm(null);
    setOpen(false);
  };

  const noPipelines = pipelines.length === 0;
  const needsLog = formScript?.needs_log; // only step 0 ever reads the log

  return (
    <section className="card schedules-panel">
      <div className="schedules-head">
        <h2>
          ⏰ Schedules <span className="muted">({schedules.length})</span>
        </h2>
        <button className="btn ghost sm" onClick={() => (open ? setOpen(false) : startNew())}>
          {open ? "Cancel" : "+ New schedule"}
        </button>
      </div>
      <p className="muted small">
        Cron times run in the server's local time. Overlapping runs are skipped (not queued),
        and a fire missed while the server was down stays missed — no catch-up.
      </p>

      {open && form && (
        <form className="schedule-form" onSubmit={submit}>
          <label>
            name
            <input type="text" maxLength={80} required value={form.name}
                   onChange={(e) => set("name", e.target.value)} placeholder="nightly errors" />
          </label>
          <label>
            cron
            <input type="text" required value={form.cron} className="mono"
                   onChange={(e) => set("cron", e.target.value)} placeholder="0 8 * * *" />
          </label>
          <div className="chips preset-chips">
            {PRESETS.map((p) => (
              <button key={p.cron} type="button" className="chip"
                      title={p.cron} onClick={() => set("cron", p.cron)}>{p.label}</button>
            ))}
          </div>
          <div className="mode-toggle schedule-what" role="group" aria-label="what runs">
            <button
              type="button"
              className={cx(form.what === "pipeline" && "on")}
              disabled={noPipelines}
              title={noPipelines ? "No saved pipelines yet — save one from the run bar (💾)" : undefined}
              onClick={() => set("what", "pipeline")}
            >Pipeline</button>
            <button
              type="button"
              className={cx(form.what === "script" && "on")}
              onClick={() => set("what", "script")}
            >Script</button>
          </div>
          {form.what === "pipeline" ? (
            <label>
              pipeline
              <select value={form.pipeline} onChange={(e) => set("pipeline", e.target.value)}>
                {pipelines.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
              </select>
            </label>
          ) : (
            <label>
              script
              <select value={form.script} onChange={(e) => pickScript(e.target.value)}>
                <option value="">— pick a script —</option>
                {scripts.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}{s.needs_log ? " · needs log" : ""}</option>
                ))}
              </select>
            </label>
          )}
          <label>
            log
            <select value={form.log} onChange={(e) => set("log", e.target.value)}>
              <option value="">— none (log-free) —</option>
              {logs.map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
            </select>
            <input
              type="text" className="mono" spellCheck="false" autoComplete="off"
              placeholder="…or type a path — e.g. /var/log/system.log"
              title="Overrides the select while filled. Absolute paths reach outside the logs directory; a relative name resolves inside it."
              value={form.logPath}
              onChange={(e) => set("logPath", e.target.value)} />
          </label>
          {form.what === "script" && needsLog && !form.log && !form.logPath.trim() && (
            <p className="chip warn schedule-log-warn">
              “{formScript.name}” reads its input through {"{log}"} — pick a log or type a path for this schedule.
            </p>
          )}
          <label>
            label <span className="muted">(optional)</span>
            <input type="text" maxLength={80} value={form.label}
                   onChange={(e) => set("label", e.target.value)} placeholder="defaults to the name" />
          </label>
          <div className="schedule-form-actions">
            <label className="inline-opt">
              <input type="checkbox" checked={form.enabled !== false}
                     onChange={(e) => set("enabled", e.target.checked)} /> enabled
            </label>
            <button className="btn primary" type="submit" disabled={!form.cron.trim() || !form.name.trim()
              || (form.what === "pipeline" ? noPipelines : !formScript)}>💾 Save schedule</button>
          </div>
          {form.what === "script" && formScript && (
            <div className="schedule-param">
              <ParamForm
                script={formScript}
                params={form.params}
                onChange={setParam}
                onApply={applyParams}
                onRemove={() => pickScript("")}
              />
            </div>
          )}
        </form>
      )}

      {schedules.length > 0 ? (
        <table className="schedule-table">
          <thead>
            <tr>
              <th>Name</th><th>When</th><th>Runs</th><th>Log</th>
              <th>Next fire</th><th>Last run</th><th></th>
            </tr>
          </thead>
          <tbody>
            {schedules.map((s) => (
              <tr key={s.name} className={cx(!s.enabled && "paused-row")}>
                <td>
                  <strong>{s.name}</strong>
                  {!s.enabled && <span className="badge">paused</span>}
                </td>
                <td>
                  <span className="mono">{s.cron}</span>
                  <span className="muted small block">{s.cron_desc}</span>
                </td>
                <td>
                  {s.pipeline
                    ? <>{s.pipeline} <span className="muted small">pipeline</span></>
                    : <span className="mono">{s.target || "—"}</span>}
                </td>
                <td className="mono">{s.log || "—"}</td>
                <td className="muted">{s.enabled ? fmtDate(s.next_due) : "—"}</td>
                <td>
                  {s.last_run_at && <span className="muted small block">{fmtTime(s.last_run_at)}</span>}
                  <span className="muted small">{s.last_result || "never run"}</span>
                </td>
                <td className="row-actions">
                  <button className="btn ghost sm" title="Run this schedule now"
                          onClick={() => onRun(s.name)}>▶</button>
                  <button className="btn ghost sm"
                          title={s.enabled ? "Pause — stop firing on the clock" : "Resume firing"}
                          onClick={() => onToggle(s.name)}>{s.enabled ? "⏸" : "⏵"}</button>
                  <button className="btn ghost sm" title="Delete this schedule"
                          onClick={() => onDelete(s.name)}>🗑</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        !open && <p className="muted">No schedules yet — set a saved pipeline, or a single script, to run on the clock.</p>
      )}
    </section>
  );
}
