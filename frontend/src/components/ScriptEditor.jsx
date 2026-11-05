import { useEffect, useState } from "react";
import { api } from "../api.js";
import { commandNeedsLog, ensureLog, stripLog } from "../command.js";
import { Modal } from "./ui.jsx";

const LANGUAGES = [
  { ext: "py", label: "Python" },
  { ext: "sh", label: "Bash" },
  { ext: "bash", label: "Bash" },
  { ext: "zsh", label: "Zsh" },
  { ext: "js", label: "Node" },
  { ext: "rb", label: "Ruby" },
  { ext: "pl", label: "Perl" },
];

const STARTERS = {
  py: `#!/usr/bin/env python3
"""Describe what this script does."""
import sys

# The log (or the previous chain step's output) is argv[1].
# Artifacts: write files into the current directory (the job's outputs/).
with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
    for line in fh:
        print(line, end="")
`,
  sh: `#!/usr/bin/env bash
# Describe what this script does.
# The log (or the previous chain step's output) is $1.
# Artifacts: write files into the current directory (the job's outputs/).
cat "$1"
`,
  bash: `#!/usr/bin/env bash
# Describe what this script does.
# The log (or the previous chain step's output) is $1.
# Artifacts: write files into the current directory (the job's outputs/).
cat "$1"
`,
  zsh: `#!/usr/bin/env zsh
# Describe what this script does.
# The log (or the previous chain step's output) is $1.
# Artifacts: write files into the current directory (the job's outputs/).
cat "$1"
`,
  js: `#!/usr/bin/env node
// The log (or the previous chain step's output) is argv[2].
const fs = require("fs");
process.stdout.write(fs.readFileSync(process.argv[2], "utf8"));
`,
  rb: `# The log (or the previous chain step's output) is ARGV[0].
ARGF.each { |line| print line }
`,
  pl: `#!/usr/bin/perl
# The log (or the previous chain step's output) is $ARGV[0].
while (<>) { print; }
`,
};

const PARAM_EXAMPLE = `[
  {
    "name": "pattern",
    "flag": "--pattern",
    "label": "Regex",
    "type": "string",
    "default": "",
    "required": true,
    "help": "What to search for"
  }
]`;

const DEFAULTS = {
  id: "",
  extension: "py",
  name: "",
  description: "",
  category: "",
  command: "",
  timeout: 600,
  output_filename: "",
  code: "",
};

export default function ScriptEditor({ scriptId, onClose, onSaved, onDeleted, onRan, onError,
                                       me = null, multiUser = false }) {
  const [form, setForm] = useState(DEFAULTS);
  const [paramsText, setParamsText] = useState("[]");
  const [paramsError, setParamsError] = useState(null);
  const [busy, setBusy] = useState(scriptId ? true : false);
  const [saving, setSaving] = useState(false);
  const [dup, setDup] = useState(false); // editing a copy that will be saved as a new script
  const [origForm, setOrigForm] = useState(null); // the form as it was before duplicating
  const isNew = !scriptId;
  const isCreate = isNew || dup;
  // The visibility choice (multi-user only): what the script is now, and
  // what the user picked. wasShared is the saved state — a shared script
  // goes private only through an admin's unshare, so the flip is locked
  // for everyone else.
  const [visibility, setVisibility] = useState("private");
  const [wasShared, setWasShared] = useState(false);
  const sharedLocked = wasShared && me?.role !== "admin";

  useEffect(() => {
    if (isNew) {
      setForm({ ...DEFAULTS, code: STARTERS.py });
      setVisibility("private");            // creations start private
      setWasShared(false);
      return;
    }
    let cancelled = false;
    api.getScript(scriptId)
      .then((src) => {
        if (cancelled) return;
        setForm({
          id: src.id,
          extension: src.extension || "py",
          name: src.manifest.name || "",
          description: src.manifest.description || "",
          category: src.manifest.category || "",
          command: src.manifest.command || "",
          timeout: src.manifest.timeout ?? 600,
          output_filename: src.manifest.output_filename || "",
          code: src.code,
        });
        setVisibility(src.owner == null ? "shared" : "private");
        setWasShared(src.owner == null);
        setParamsText(JSON.stringify(src.manifest.parameters || [], null, 2));
      })
      .catch((e) => !cancelled && (onError(e), onClose()))
      .finally(() => !cancelled && setBusy(false));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scriptId]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  // The target-file choice, derived from the command: {log} present — or
  // blank, meaning the extension default — means the script reads the log.
  const needsLog = commandNeedsLog(form.command);
  const setCommand = (command) => setForm((f) => ({ ...f, command }));

  const changeLanguage = (e) => {
    const extension = e.target.value;
    setForm((f) => ({
      ...f,
      extension,
      // keep the starter template while it is untouched
      code: Object.values(STARTERS).includes(f.code.trim() + "\n") || f.code === ""
        ? STARTERS[extension]
        : f.code,
    }));
  };

  const save = async ({ silent = false } = {}) => {
    let parameters;
    try {
      parameters = JSON.parse(paramsText || "[]");
      if (!Array.isArray(parameters)) throw new Error("must be a JSON array");
    } catch (e) {
      setParamsError(`Parameters: ${e.message}`);
      return null;
    }
    setParamsError(null);
    setSaving(true);
    try {
      const body = {
        id: form.id.trim(),
        extension: form.extension,
        code: form.code,
        manifest: {
          name: form.name.trim() || form.id.trim(),
          description: form.description.trim(),
          category: form.category.trim(),
          command: form.command.trim(),
          timeout: Number(form.timeout) || 600,
          parameters,
          output_filename: form.output_filename.trim() || null,
        },
      };
      const res = isCreate
        ? await api.createScript(body)
        : await api.updateScript(scriptId, body);
      if (multiUser) {
        // the visibility choice rides on save: the same share/unshare the
        // script cards perform, folded into one click
        const shared = res.script.owner == null;
        if (visibility === "shared" && !shared) {
          await api.shareScript(res.script.id);
          res.script.owner = null;          // it's in the shared library now
        } else if (visibility === "private" && shared && me?.role === "admin") {
          await api.unshareScript(res.script.id);
          res.script.owner = me.username;   // unshare moves it to the admin
        }
      }
      if (!silent) onSaved(res.script);
      return { script: res.script, parameters };
    } catch (e) {
      onError(e);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const duplicate = () => {
    setOrigForm(form);
    setDup(true);
    setForm((f) => ({
      ...f,
      id: `${f.id}_copy`.slice(0, 64),
      name: f.name ? `${f.name} (copy)` : "",
    }));
  };

  const undoDuplicate = () => {
    setDup(false);
    if (origForm) setForm(origForm);
    setOrigForm(null);
  };

  const remove = async () => {
    if (!window.confirm(
      `Delete script "${form.name || form.id}"?\n\nThe code and manifest files are permanently removed. Job history is kept, but jobs of this script can no longer be re-run.`)) {
      return;
    }
    try {
      await api.deleteScript(scriptId);
      onDeleted(form.id, form.name || form.id);
    } catch (e) {
      onError(e);
    }
  };

  const idPattern = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
  const idOk = idPattern.test(form.id.trim());

  const [copied, setCopied] = useState(false);
  const [logs, setLogs] = useState(null); // null = not loaded yet
  const [logPath, setLogPath] = useState("");
  const [runOpen, setRunOpen] = useState(false);
  const [running, setRunning] = useState(false);

  const openRun = () => {
    setRunOpen(true);
    if (logs === null) {
      api.listLogs().then((r) => {
        setLogs(r.logs);
        // a log-free script defaults to no file — that's its case
        setLogPath(commandNeedsLog(form.command) ? (r.logs[0]?.path || "") : "");
      }).catch(onError);
    }
  };

  /** Save (silently), then run the script with its manifest defaults on the
   *  chosen log — or with nothing selected, when it was saved log-free. */
  const runOn = async (path) => {
    if (!path && needsLog) return;
    setRunning(true);
    try {
      const saved = await save({ silent: true });
      if (!saved) return;
      const params = {};
      for (const spec of saved.parameters) {
        if (spec.default != null && spec.default !== "") params[spec.name] = spec.default;
      }
      const res = await api.createJobs({
        log_path: path,
        mode: "parallel",
        jobs: [{ script_id: saved.script.id, params }],
      });
      onRan(res.jobs[0].id);
    } catch (e) {
      onError(e);
    } finally {
      setRunning(false);
    }
  };
  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(form.code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — the download button is the fallback */
    }
  };

  return (
    <Modal title={isNew ? "＋ New script" : dup ? `⧉ Duplicate of ${form.name || scriptId}` : `✏️ ${form.name || scriptId}`} onClose={onClose} wide>
      <div className="script-editor">
        <div className="editor-grid">
          <label className="editor-field">
            <span>Script id <span className="muted">(file name)</span></span>
            <input
              className="editor-input mono"
              value={form.id}
              onChange={set("id")}
              disabled={!isCreate}
              placeholder="http_404_counter"
              maxLength={64}
            />
            {!idOk && form.id && (
              <span className="field-error">letters, digits, '-' and '_' only; must start alphanumeric</span>
            )}
          </label>
          <label className="editor-field">
            <span>Language</span>
            <select className="editor-input" value={form.extension} onChange={changeLanguage}>
              {LANGUAGES.map((l) => (
                <option key={l.ext} value={l.ext}>{l.label} (.{l.ext})</option>
              ))}
            </select>
          </label>
          <label className="editor-field">
            <span>Display name</span>
            <input className="editor-input" value={form.name} onChange={set("name")}
                  placeholder="HTTP 404 Counter" />
          </label>
          <label className="editor-field">
            <span>Timeout (seconds)</span>
            <input className="editor-input" type="number" min="1" max="86400"
                  value={form.timeout} onChange={set("timeout")} />
          </label>
          <label className="editor-field span-2">
            <span>Description</span>
            <input className="editor-input" value={form.description} onChange={set("description")}
                  placeholder="What this script does — shown on the script card" />
          </label>
          <label className="editor-field">
            <span>Category <span className="muted">(optional)</span></span>
            <input className="editor-input" value={form.category} onChange={set("category")}
                  placeholder="web" title="Groups the script under a filter chip in the picker" />
            <span className="field-hint">
              Scripts with the same category share a filter chip — e.g. “web”, “counting”
            </span>
          </label>
          <label className="editor-field span-2">
            <span>Command template</span>
            <input className="editor-input mono" value={form.command} onChange={set("command")}
                  placeholder={`${form.extension === "py" ? "python3" : "bash"} {script} {log} {args}`} />
            <span className="field-hint">
              Placeholders: {"{script}"} {"{log}"} {"{output}"} {"{args}"} — {"{log}"} is always
              the plain, uncompressed input (.gz/.bz2/.xz are decompressed first) — leave empty
              for the default
            </span>
          </label>
          <div className="editor-field span-2">
            <span>Target file</span>
            <div className="target-choices">
              <label>
                <input type="radio" name="target-file" checked={needsLog}
                       onChange={() => setCommand(ensureLog(form.command))} />
                selected log — the command reads {"{log}"}
              </label>
              <label>
                <input type="radio" name="target-file" checked={!needsLog}
                       onChange={() => setCommand(stripLog(form.command, form.extension))} />
                none (log-free) — {"{log}"} is removed from the command
              </label>
            </div>
          </div>
          <label className="editor-field span-2">
            <span>Output file name <span className="muted">(optional — enables the {"{output}"} placeholder)</span></span>
            <input className="editor-input mono" value={form.output_filename} onChange={set("output_filename")}
                  placeholder="report.md" />
          </label>
          <label className="editor-field span-2">
            <span>Code</span>
            <textarea
              className="editor-textarea code"
              value={form.code}
              onChange={set("code")}
              spellCheck={false}
              rows={14}
            />
            <span className="field-hint">
              The log (or previous chain step's output) is argv[1] / $1 · cwd is the job's outputs/ dir
            </span>
          </label>
          <label className="editor-field span-2">
            <span>Parameters <span className="muted">(JSON array — renders as a form in the UI)</span></span>
            <textarea
              className="editor-textarea mono"
              value={paramsText}
              onChange={(e) => setParamsText(e.target.value)}
              spellCheck={false}
              rows={7}
              placeholder={PARAM_EXAMPLE}
            />
            {paramsError
              ? <span className="field-error">{paramsError}</span>
              : <span className="field-hint">
                  name · flag · label · type (string|integer|float|boolean|enum|multi) ·
                  choices · default · required · help
                </span>}
          </label>
          {multiUser && (
            <div className="editor-field span-2">
              <span>Visibility</span>
              <div className="target-choices">
                <label>
                  <input type="radio" name="visibility" checked={visibility === "private"}
                         onChange={() => setVisibility("private")} />
                  private — only you (and admins) can see and run it
                </label>
                <label>
                  <input type="radio" name="visibility" checked={visibility === "shared"}
                         disabled={sharedLocked} onChange={() => setVisibility("shared")} />
                  shared — everyone can see and run it
                </label>
              </div>
              <span className="field-hint">
                {sharedLocked
                  ? "This script is shared; only an admin can make it private again."
                  : me?.role === "admin"
                    ? "Making a shared script private moves it into your own private scripts — other users lose access."
                    : "Sharing is one-way for you — only an admin can pull a script back out of the shared library."}
              </span>
            </div>
          )}
        </div>
        {runOpen && (
          <div className="run-strip">
            <span className="muted sm">Run on:</span>
            <select
              className="editor-input run-log-select"
              value={logPath}
              onChange={(e) => setLogPath(e.target.value)}
            >
              {!needsLog && <option value="">— none (log-free) —</option>}
              {logs === null
                ? <option value="">loading logs…</option>
                : logs.length === 0
                  ? <option value="">{needsLog ? "(no logs available)" : "(no logs — none needed)"}</option>
                  : logs.map((l) => <option key={l.path} value={l.path}>{l.name}</option>)}
            </select>
            <button
              className="btn primary sm"
              disabled={(needsLog && !logPath) || running || saving || busy || !idOk || !form.code.trim()}
              onClick={() => runOn(logPath)}
              title={needsLog ? "Save, then run on the chosen log"
                              : "Save, then run with no file selected"}
            >
              {running ? "Starting…" : "▶ Run now"}
            </button>
            <button className="btn ghost sm" onClick={() => setRunOpen(false)}>✕</button>
          </div>
        )}
        <div className="editor-actions">
          {!isNew && !dup && (
            <button className="btn ghost sm" onClick={duplicate}
                    title="Prefill this script as a new one — original untouched">⧉ Duplicate</button>
          )}
          {dup && (
            <button className="btn ghost sm" onClick={undoDuplicate}
                    title="Back to editing the original script">↩ Editing original</button>
          )}
          {!isNew && (
            <button className="btn danger sm" onClick={remove}>🗑 Delete script</button>
          )}
          <span className="spacer" />
          <button className="btn ghost" onClick={copyCode} disabled={!form.code.trim()}>
            {copied ? "Copied ✓" : "⧉ Copy code"}
          </button>
          {!isNew && (
            <a
              className="btn ghost"
              href={`/api/scripts/${scriptId}/export`}
              download
              title="Download code + manifest as a tar.gz — your backup before deleting"
            >
              ⬇ Backup
            </a>
          )}
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button
            className="btn ghost"
            onClick={openRun}
            disabled={busy || saving || !idOk || !form.code.trim()}
            title="Save this script and run it on a log — see the result immediately"
          >
            ▶ Run…
          </button>
          <button className="btn" onClick={save} disabled={busy || saving || !idOk || !form.code.trim()}>
            {saving ? "Saving…" : (isCreate ? "Create script" : "Save changes")}
          </button>
        </div>
      </div>
    </Modal>
  );
}
