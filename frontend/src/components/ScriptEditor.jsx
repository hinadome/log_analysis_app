import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Modal } from "./ui.jsx";

const LANGUAGES = [
  { ext: "py", label: "Python" },
  { ext: "sh", label: "Bash" },
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
  command: "",
  timeout: 600,
  output_filename: "",
  code: "",
};

export default function ScriptEditor({ scriptId, onClose, onSaved, onDeleted, onError }) {
  const [form, setForm] = useState(DEFAULTS);
  const [paramsText, setParamsText] = useState("[]");
  const [paramsError, setParamsError] = useState(null);
  const [busy, setBusy] = useState(scriptId ? true : false);
  const [saving, setSaving] = useState(false);
  const isNew = !scriptId;

  useEffect(() => {
    if (isNew) {
      setForm({ ...DEFAULTS, code: STARTERS.py });
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
          command: src.manifest.command || "",
          timeout: src.manifest.timeout ?? 600,
          output_filename: src.manifest.output_filename || "",
          code: src.code,
        });
        setParamsText(JSON.stringify(src.manifest.parameters || [], null, 2));
      })
      .catch((e) => !cancelled && (onError(e), onClose()))
      .finally(() => !cancelled && setBusy(false));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scriptId]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

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

  const save = async () => {
    let parameters;
    try {
      parameters = JSON.parse(paramsText || "[]");
      if (!Array.isArray(parameters)) throw new Error("must be a JSON array");
    } catch (e) {
      setParamsError(`Parameters: ${e.message}`);
      return;
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
          command: form.command.trim(),
          timeout: Number(form.timeout) || 600,
          parameters,
          output_filename: form.output_filename.trim() || null,
        },
      };
      const res = isNew
        ? await api.createScript(body)
        : await api.updateScript(scriptId, body);
      onSaved(res.script);
    } catch (e) {
      onError(e);
    } finally {
      setSaving(false);
    }
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

  return (
    <Modal title={isNew ? "＋ New script" : `✏️ ${form.name || scriptId}`} onClose={onClose} wide>
      <div className="script-editor">
        <div className="editor-grid">
          <label className="editor-field">
            <span>Script id <span className="muted">(file name)</span></span>
            <input
              className="editor-input mono"
              value={form.id}
              onChange={set("id")}
              disabled={!isNew}
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
          <label className="editor-field span-2">
            <span>Command template</span>
            <input className="editor-input mono" value={form.command} onChange={set("command")}
                  placeholder={`${form.extension === "py" ? "python3" : "bash"} {script} {log} {args}`} />
            <span className="field-hint">
              Placeholders: {"{script}"} {"{log}"} {"{output}"} {"{args}"} — leave empty for the default
            </span>
          </label>
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
        </div>
        <div className="editor-actions">
          {!isNew && (
            <button className="btn danger sm" onClick={remove}>🗑 Delete script</button>
          )}
          <span className="spacer" />
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn" onClick={save} disabled={busy || saving || !idOk || !form.code.trim()}>
            {saving ? "Saving…" : (isNew ? "Create script" : "Save changes")}
          </button>
        </div>
      </div>
    </Modal>
  );
}
