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

/** Saved chains moved to the server (api.savePipeline — shared by every
 *  browser and the python UI). The old localStorage copy is migrated once
 *  and then cleared; the helpers stay so the legacy key's shape lives in
 *  one place and the migration stays unit-testable. */

const LEGACY_CHAIN_KEY = "logsmith.chainPresets";

function legacyChainPresets() {
  try {
    return JSON.parse(localStorage.getItem(LEGACY_CHAIN_KEY) || "{}");
  } catch {
    return {};
  }
}

/** Move localStorage-saved chains to the server. Pipelines whose name
 *  already exists on the server are left alone (the server's wins).
 *  Returns how many were migrated; on any failure the local copy is kept
 *  so the next page load can try again. */
export async function migrateLegacyChains({ listPipelines, savePipeline }) {
  const legacy = legacyChainPresets();
  const names = Object.keys(legacy);
  if (!names.length) return 0;
  const existing = new Set((await listPipelines()).pipelines.map((p) => p.name));
  let moved = 0;
  for (const name of names) {
    if (existing.has(name)) continue;
    const steps = (legacy[name].steps || [])
      .map(({ scriptId, params }) => ({ script_id: scriptId, params }));
    if (!steps.length) continue;
    await savePipeline({ name, steps });
    moved += 1;
  }
  localStorage.removeItem(LEGACY_CHAIN_KEY);
  return moved;
}
