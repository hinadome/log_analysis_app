import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { keyPlaceholder, testMessage, validateSettings } from "../llm.js";

/**
 * Admin settings — the LLM provider switch. Pick the active provider,
 * fill its api key / model / base URL, save, and test the wire before
 * trusting it. Every configured provider is remembered server-side, so
 * switching back never loses a key.
 *
 * Keys never render (the server sends a mask): a blank key field means
 * "keep the stored one". The server enforces admin rights — a regular
 * account landing here by URL just sees its 403 as the error banner.
 */
export default function SettingsPanel({ onBack }) {
  const [meta, setMeta] = useState(null);   // server view: provider list + masks
  const [form, setForm] = useState({});       // local fields per provider key
  const [provider, setProvider] = useState("none");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    api.llmSettings()
      .then((s) => {
        setMeta(s);
        setProvider(s.provider);
        const fields = {};
        for (const p of s.providers) {
          fields[p.key] = { api_key: "", model: p.model, base_url: p.base_url };
        }
        setForm(fields);
      })
      .catch((e) => setError(String(e.message || e)));
  }, []);

  const setField = (key, name, value) =>
    setForm((f) => ({ ...f, [key]: { ...f[key], [name]: value } }));

  // after a save: keep what the server says (masks, defaults), keep the
  // local model/base_url edits, blank the typed keys (never echoed)
  const absorb = (s) => {
    setMeta(s);
    setProvider(s.provider);
    setForm((f) => {
      const fields = {};
      for (const p of s.providers) {
        fields[p.key] = {
          api_key: "",
          model: f[p.key]?.model ?? p.model,
          base_url: f[p.key]?.base_url ?? p.base_url,
        };
      }
      return fields;
    });
  };

  const save = useCallback(async (thenTest = false) => {
    const invalid = validateSettings(form);
    if (invalid) { setError(invalid); setNotice(null); return; }
    setError(null);
    if (thenTest) setTesting(true);
    try {
      absorb(await api.llmUpdateSettings({ provider, providers: form }));
      if (thenTest) {
        const result = await api.llmTest();
        const msg = testMessage(result);
        result.ok ? setNotice(msg) : setError(msg);
      } else {
        setNotice("Settings saved");
      }
    } catch (e) {
      setNotice(null);
      setError(String(e.message || e));
    } finally {
      setTesting(false);
    }
  }, [form, provider]);

  if (!meta) {
    return (
      <div className="card">
        <h2>Settings</h2>
        {error
          ? <p className="field-error">{error}</p>
          : <p className="muted">Loading…</p>}
        <button className="btn" onClick={onBack}>← Back</button>
      </div>
    );
  }

  return (
    <div className="card editor">
      <h2>Settings — LLM provider</h2>
      {error && <div className="flash bad">{error}</div>}
      {notice && <div className="flash ok">{notice}</div>}
      <p className="muted">
        The LLM the app calls — switch it here, no restart needed. Keys stay on
        the server (a blank key field keeps the stored one).
      </p>

      <label>Provider
        <select value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="none">None — LLM off (default)</option>
          {meta.providers.map((p) => (
            <option key={p.key} value={p.key}>{p.label}</option>
          ))}
        </select>
      </label>
      {provider === "none" && (
        <p className="muted">Nothing calls out until you pick a provider.</p>
      )}

      {meta.providers.map((p) => (
        <fieldset key={p.key} className="provider-fields"
                  style={{ display: provider === p.key ? "" : "none" }}>
          <legend>{p.label}</legend>
          <p className="muted">{p.hint}</p>
          <label>API key
            <input type="password" autoComplete="off"
                   value={form[p.key]?.api_key || ""}
                   onChange={(e) => setField(p.key, "api_key", e.target.value)}
                   placeholder={keyPlaceholder(p.api_key_masked, p.has_key)} />
          </label>
          <label>Model
            <input type="text" value={form[p.key]?.model || ""}
                   onChange={(e) => setField(p.key, "model", e.target.value)}
                   placeholder={p.wire === "openai" ? "gpt-4o-mini" : "claude-sonnet-4-5"} />
          </label>
          <label>Base URL <span className="muted">(empty = {p.default_base_url || "required — set the endpoint"})</span>
            <input type="text" className="mono" value={form[p.key]?.base_url || ""}
                   onChange={(e) => setField(p.key, "base_url", e.target.value)}
                   placeholder={p.default_base_url} />
          </label>
        </fieldset>
      ))}

      <button className="btn primary" onClick={() => save(false)}>Save</button>{" "}
      <button className="btn" disabled={testing} onClick={() => save(true)}
              title="Save, then send one tiny completion through the saved settings">
        {testing ? "Testing…" : "Save & test"}
      </button>{" "}
      <button className="btn ghost" onClick={onBack}>← Back</button>
    </div>
  );
}
