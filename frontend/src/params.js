// Parameter value shaping shared by the run flow (App) and schedules:
// defaults from the manifest, and UI string values -> JSON types for
// submission bodies.

export function defaultParams(script) {
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
export function serializeParams(script, params) {
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

/** Whether a step selection needs a log at all: in chain mode only the
 *  first step reads the log (the rest read the previous step's output);
 *  in parallel every step does. Log-free scripts run without one. */
export function selectionNeedsLog(steps, scriptById, mode) {
  return steps.some((st, i) => {
    const script = scriptById[st.scriptId];
    return Boolean(script?.needs_log) && (mode !== "chain" || i === 0);
  });
}
