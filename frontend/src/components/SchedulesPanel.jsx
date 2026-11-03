import { useState } from "react";
import { cx, fmtDate, fmtTime } from "../fmt.js";

// Cron presets the form offers as one-click starting points — they just
// fill the expression field, everything stays editable.
const PRESETS = [
  { label: "every 5 min", cron: "*/5 * * * *" },
  { label: "hourly", cron: "0 * * * *" },
  { label: "daily 08:00", cron: "0 8 * * *" },
  { label: "weekdays 09:00", cron: "0 9 * * mon-fri" },
];

/**
 * Saved schedules: a pipeline + a log + a cron expression, fired by the
 * server's ticker. Scheduled runs are ordinary jobs (labelled with the
 * schedule's name) — they show up in the Jobs panel like any other run.
 */
export default function SchedulesPanel({
  schedules, pipelines, logs, defaultLog,
  onSave, onToggle, onRun, onDelete,
}) {
  const [open, setOpen] = useState(false); // the create form starts folded
  const [form, setForm] = useState(null);  // null = not editing yet
  const set = (field, value) => setForm((f) => ({ ...f, [field]: value }));

  const startNew = () => {
    setOpen(true);
    setForm({
      name: "", cron: "", label: "",
      pipeline: pipelines[0]?.name || "",
      log: defaultLog || logs[0]?.name || "",
      enabled: true,
    });
  };

  const submit = (e) => {
    e.preventDefault();
    if (!form.name.trim() || !form.cron.trim()) return;
    onSave({
      name: form.name.trim(), cron: form.cron.trim(), label: form.label.trim() || null,
      pipeline: form.pipeline, log: form.log, enabled: form.enabled !== false,
    });
    setForm(null);
    setOpen(false);
  };

  const noPipelines = pipelines.length === 0;

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
        noPipelines ? (
          <p className="muted">
            A schedule runs a saved pipeline — save one first (💾 in the run bar above).
          </p>
        ) : (
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
            <label>
              pipeline
              <select value={form.pipeline} onChange={(e) => set("pipeline", e.target.value)}>
                {pipelines.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
              </select>
            </label>
            <label>
              log
              <select value={form.log} onChange={(e) => set("log", e.target.value)}>
                {logs.map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
              </select>
            </label>
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
              <button className="btn primary" type="submit">💾 Save schedule</button>
            </div>
          </form>
        )
      )}

      {schedules.length > 0 ? (
        <table className="schedule-table">
          <thead>
            <tr>
              <th>Name</th><th>When</th><th>Pipeline</th><th>Log</th>
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
                <td>{s.pipeline}</td>
                <td className="mono">{s.log}</td>
                <td className="muted">{s.enabled ? fmtDate(s.next_due) : "—"}</td>
                <td>
                  {s.last_run_at && <span className="muted small block">{fmtTime(s.last_run_at)}</span>}
                  <span className="muted small">{s.last_result || "never run"}</span>
                </td>
                <td className="row-actions">
                  <button className="btn ghost sm" title="Run this schedule's pipeline now"
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
        !open && <p className="muted">No schedules yet — set a saved pipeline to run on a log, on the clock.</p>
      )}
    </section>
  );
}
