// The one-time localStorage → server migration of saved chains.
// presets.js touches localStorage lazily inside its functions, so a plain
// object stub is enough in the node test environment.
import { afterEach, describe, expect, it } from "vitest";

import { migrateLegacyChains } from "./presets.js";

function stubStorage(initial = {}) {
  const data = { ...initial };
  globalThis.localStorage = {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
  };
  return data;
}

afterEach(() => {
  delete globalThis.localStorage;
});

describe("migrateLegacyChains", () => {
  it("moves saved chains to the server and clears the local copy", async () => {
    const data = stubStorage({
      "scriptengine.chainPresets": JSON.stringify({
        "errors then count": {
          steps: [
            { scriptId: "grep_search", params: { pattern: "ERROR" } },
            { scriptId: "error_counter", params: { level: "ERROR" } },
          ],
          saved_at: "2026-10-01T00:00:00Z",
        },
      }),
    });
    const saved = [];
    const api = {
      listPipelines: async () => ({ pipelines: [] }),
      savePipeline: async (body) => { saved.push(body); },
    };

    const moved = await migrateLegacyChains(api);

    expect(moved).toBe(1);
    expect(saved).toEqual([{
      name: "errors then count",
      steps: [
        { script_id: "grep_search", params: { pattern: "ERROR" } },
        { script_id: "error_counter", params: { level: "ERROR" } },
      ],
    }]);
    expect(data["scriptengine.chainPresets"]).toBeUndefined();
  });

  it("leaves names that already exist on the server alone", async () => {
    const data = stubStorage({
      "scriptengine.chainPresets": JSON.stringify({
        mine: { steps: [{ scriptId: "grep_search", params: {} }] },
        theirs: { steps: [{ scriptId: "error_counter", params: {} }] },
      }),
    });
    const saved = [];
    const api = {
      listPipelines: async () => ({ pipelines: [{ name: "theirs" }] }),
      savePipeline: async (body) => { saved.push(body); },
    };

    const moved = await migrateLegacyChains(api);

    expect(moved).toBe(1);
    expect(saved.map((b) => b.name)).toEqual(["mine"]);
    // the whole legacy key is cleared once every name is accounted for
    expect(data["scriptengine.chainPresets"]).toBeUndefined();
  });

  it("keeps the local copy when a save fails, so the next load retries", async () => {
    const data = stubStorage({
      "scriptengine.chainPresets": JSON.stringify({
        mine: { steps: [{ scriptId: "grep_search", params: {} }] },
      }),
    });
    const api = {
      listPipelines: async () => ({ pipelines: [] }),
      savePipeline: async () => { throw new Error("500: boom"); },
    };

    await expect(migrateLegacyChains(api)).rejects.toThrow("boom");
    expect(data["scriptengine.chainPresets"]).toBeDefined();
  });

  it("is a no-op when nothing is saved locally", async () => {
    stubStorage({});
    const api = {
      listPipelines: async () => { throw new Error("should not be called"); },
      savePipeline: async () => { throw new Error("should not be called"); },
    };
    expect(await migrateLegacyChains(api)).toBe(0);
  });
});
