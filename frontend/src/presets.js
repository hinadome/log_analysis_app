/** Parameter presets, stored per browser in localStorage. */

const KEY = "logsmith.presets";

export function loadPresets() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "{}");
  } catch {
    return {};
  }
}

function persist(all) {
  localStorage.setItem(KEY, JSON.stringify(all));
}

export function presetsFor(scriptId) {
  return loadPresets()[scriptId] || [];
}

export function savePreset(scriptId, name, params) {
  const all = loadPresets();
  const list = all[scriptId] || [];
  const next = list.filter((p) => p.name !== name);
  next.push({ name, params });
  all[scriptId] = next;
  persist(all);
}

export function deletePreset(scriptId, name) {
  const all = loadPresets();
  all[scriptId] = (all[scriptId] || []).filter((p) => p.name !== name);
  persist(all);
}

/** Saved chains (whole ordered pipelines), also stored in localStorage. */

const CHAIN_KEY = "logsmith.chainPresets";

export function loadChainPresets() {
  try {
    return JSON.parse(localStorage.getItem(CHAIN_KEY) || "{}");
  } catch {
    return {};
  }
}

/** Save the current step list under a name. steps: [{scriptId, params}] */
export function saveChainPreset(name, steps) {
  const all = loadChainPresets();
  all[name] = { steps, saved_at: new Date().toISOString() };
  localStorage.setItem(CHAIN_KEY, JSON.stringify(all));
}

export function deleteChainPreset(name) {
  const all = loadChainPresets();
  delete all[name];
  localStorage.setItem(CHAIN_KEY, JSON.stringify(all));
}
