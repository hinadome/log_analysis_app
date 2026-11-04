// Submission shaping shared by the run flow and schedules: defaults,
// serialization to JSON types, and the needs-a-log rule.
import { describe, expect, it } from "vitest";

import { defaultParams, selectionNeedsLog, serializeParams } from "./params.js";

const SCRIPT = {
  id: "error_counter",
  parameters: [
    { name: "level", type: "enum", choices: ["ERROR", "WARN"], default: "ERROR" },
    { name: "pattern", type: "string", default: "" },
    { name: "json", type: "boolean", default: false },
    { name: "limit", type: "integer", default: 10 },
    { name: "ratio", type: "float", default: 0.5 },
    { name: "fields", type: "multi", default: ["a", "b"] },
  ],
};

describe("defaultParams", () => {
  it("seeds every parameter from its manifest default", () => {
    expect(defaultParams(SCRIPT)).toEqual({
      level: "ERROR",
      pattern: "",
      json: false,
      limit: 10,
      ratio: 0.5,
      fields: ["a", "b"],
    });
  });

  it("falls back per type when a default is missing", () => {
    expect(defaultParams({
      parameters: [
        { name: "a", type: "enum", choices: ["x"], default: null },
        { name: "b", type: "multi", default: "not-a-list" },
        { name: "c", type: "boolean", default: undefined },
        { name: "d", type: "string" },
      ],
    })).toEqual({ a: "x", b: [], c: false, d: "" });
  });
});

describe("serializeParams", () => {
  it("coerces numeric strings and drops empties", () => {
    expect(serializeParams(SCRIPT, {
      level: "WARN", pattern: "", json: "true", limit: "5", ratio: "0.25",
    })).toEqual({ level: "WARN", json: "true", limit: 5, ratio: 0.25 });
  });

  it("passes multi values through as arrays", () => {
    expect(serializeParams(SCRIPT, { fields: ["x", "y"] }))
      .toEqual({ fields: ["x", "y"] });
  });
});

describe("selectionNeedsLog", () => {
  const byId = {
    analyzer: { id: "analyzer", needs_log: true },
    disk: { id: "disk", needs_log: false },
  };

  it("parallel: any needs-log step forces a log", () => {
    expect(selectionNeedsLog([{ scriptId: "disk" }], byId, "parallel")).toBe(false);
    expect(selectionNeedsLog([{ scriptId: "disk" }, { scriptId: "analyzer" }], byId, "parallel")).toBe(true);
  });

  it("chain: only step 1 reads the log — later steps eat the previous output", () => {
    expect(selectionNeedsLog(
      [{ scriptId: "analyzer" }, { scriptId: "analyzer" }], byId, "chain")).toBe(true);
    // a log-free first step feeding a needs-log second step is fine
    expect(selectionNeedsLog(
      [{ scriptId: "disk" }, { scriptId: "analyzer" }], byId, "chain")).toBe(false);
    expect(selectionNeedsLog(
      [{ scriptId: "disk" }, { scriptId: "disk" }], byId, "chain")).toBe(false);
  });

  it("tolerates steps whose script vanished", () => {
    expect(selectionNeedsLog([{ scriptId: "gone" }], byId, "parallel")).toBe(false);
  });
});
