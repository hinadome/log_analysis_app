import { useState } from "react";
import { cx } from "../fmt.js";
import { deletePreset, presetsFor, savePreset } from "../presets.js";

function PresetBar({ script, params, onApply }) {
  const [version, setVersion] = useState(0); // bump to re-read localStorage
  const [name, setName] = useState("");
  const presets = presetsFor(script.id);
  void version;

  const save = () => {
    const finalName = (name || "").trim() || window.prompt("Preset name:");
    if (!finalName) return;
    savePreset(script.id, finalName, params);
    setName(finalName);
    setVersion((v) => v + 1);
  };

  const remove = () => {
    const current = name || (presets[0] && presets[0].name);
    if (!current) return;
    deletePreset(script.id, current);
    setName("");
    setVersion((v) => v + 1);
  };

  return (
    <div className="preset-bar">
      <select
        value=""
        onChange={(e) => {
          const preset = presets.find((p) => p.name === e.target.value);
          if (preset) {
            onApply(preset.params);
            setName(preset.name);
          }
        }}
      >
        <option value="">
          {presets.length === 0 ? "No presets yet" : `Load preset (${presets.length})`}
        </option>
        {presets.map((p) => (
          <option key={p.name} value={p.name}>{p.name}</option>
        ))}
      </select>
      <input
        className="preset-name"
        placeholder="preset name"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <button className="btn sm" onClick={save} title="Save current values as a preset">Save</button>
      <button className="btn ghost sm" onClick={remove} disabled={presets.length === 0}>Delete</button>
    </div>
  );
}

function Field({ spec, value, onChange }) {
  const common = { value: value ?? "", onChange: (e) => onChange(e.target.value) };

  let input;
  switch (spec.type) {
    case "boolean":
      input = (
        <input
          type="checkbox"
          checked={!!value}
          onChange={(e) => onChange(e.target.checked)}
        />
      );
      break;
    case "enum":
      input = (
        <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
          {spec.choices.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      );
      break;
    case "integer":
    case "float":
      input = (
        <input
          type="number"
          step={spec.type === "float" ? "any" : "1"}
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder="default"
        />
      );
      break;
    case "multi":
      input = (
        <input
          type="text"
          value={Array.isArray(value) ? value.join(", ") : value ?? ""}
          onChange={(e) =>
            onChange(e.target.value.split(",").map((s) => s.trim()).filter(Boolean))
          }
          placeholder="comma-separated values"
        />
      );
      break;
    default:
      input = (
        <input
          type="text"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder={
            spec.default !== null && spec.default !== undefined && spec.default !== ""
              ? `default: ${spec.default}`
              : "optional"
          }
        />
      );
  }

  return (
    <label className={cx("field", { "field-bool": spec.type === "boolean" })}>
      <span className="field-label">
        <span>
          {spec.label || spec.name}
          {spec.required && <span className="req"> *</span>}
        </span>
        <span className="field-flag mono">{spec.flag}</span>
      </span>
      {input}
      {spec.help && <span className="field-help">{spec.help}</span>}
    </label>
  );
}

export default function ParamForm({
  script, params, onChange, onApply, onRemove,
  stepNumber = null, chainMode = false, canMoveUp = false, canMoveDown = false,
  onMoveUp, onMoveDown,
}) {
  return (
    <div className="param-card">
      <div className="param-card-head">
        <div>
          <div className="param-card-title">
            {stepNumber != null && <span className="step-chip">{stepNumber}</span>}
            {script.name}
          </div>
          <div className="muted sm">
            {chainMode
              ? stepNumber === 1
                ? "input: selected log file"
                : "input: previous step's output"
              : script.description}
          </div>
        </div>
        <div className="param-card-actions">
          {stepNumber != null && (
            <>
              <button className="btn ghost sm" disabled={!canMoveUp}
                      title="Move earlier" onClick={onMoveUp}>↑</button>
              <button className="btn ghost sm" disabled={!canMoveDown}
                      title="Move later" onClick={onMoveDown}>↓</button>
            </>
          )}
          <button className="btn ghost sm" onClick={onRemove}>Remove</button>
        </div>
      </div>
      <PresetBar script={script} params={params} onApply={onApply} />
      {script.parameters.length === 0 && (
        <div className="muted sm">No parameters — runs with defaults.</div>
      )}
      <div className="param-grid">
        {script.parameters.map((spec) => (
          <Field
            key={spec.name}
            spec={spec}
            value={params[spec.name]}
            onChange={(v) => onChange(spec.name, v)}
          />
        ))}
      </div>
    </div>
  );
}
