// Pure UI logic locked in by tests: the LCS line diff and the table detector.
import { describe, expect, it } from "vitest";

import { lineDiff } from "./components/DiffView.jsx";
import { tableRows } from "./components/ui.jsx";

describe("lineDiff", () => {
  it("marks identical inputs as all-same", () => {
    const d = lineDiff("a\nb\nc", "a\nb\nc");
    expect(d.rows.map((r) => r.type)).toEqual(["same", "same", "same"]);
    expect(d.added).toBe(0);
    expect(d.removed).toBe(0);
    expect(d.truncated).toBe(false);
  });

  it("detects a pure insertion", () => {
    const d = lineDiff("a\nc", "a\nb\nc");
    expect(d.rows).toEqual([
      { type: "same", text: "a" },
      { type: "add", text: "b" },
      { type: "same", text: "c" },
    ]);
    expect(d.added).toBe(1);
    expect(d.removed).toBe(0);
  });

  it("detects a deletion", () => {
    const d = lineDiff("a\nb\nc", "a\nc");
    expect(d.rows).toEqual([
      { type: "same", text: "a" },
      { type: "del", text: "b" },
      { type: "same", text: "c" },
    ]);
    expect(d.removed).toBe(1);
  });

  it("splits a modified line into del + add", () => {
    const d = lineDiff("a\nx\nc", "a\ny\nc");
    expect(d.rows).toEqual([
      { type: "same", text: "a" },
      { type: "del", text: "x" },
      { type: "add", text: "y" },
      { type: "same", text: "c" },
    ]);
  });

  it("handles empty sides and a single trailing newline", () => {
    expect(lineDiff("", "x\n").rows).toEqual([{ type: "add", text: "x" }]);
    expect(lineDiff("x\n", "").rows).toEqual([{ type: "del", text: "x" }]);
    expect(lineDiff("", "").rows).toEqual([]);
  });

  it("flags truncation past 2000 lines but still diffs the prefix", () => {
    const big = Array.from({ length: 2500 }, (_, i) => `line ${i}`).join("\n");
    const d = lineDiff(big, big);
    expect(d.truncated).toBe(true);
    expect(d.same).toBe(2000);
    expect(d.added).toBe(0);
    expect(d.removed).toBe(0);
  });
});

describe("tableRows", () => {
  it("accepts a non-empty array of objects", () => {
    const rows = [{ a: 1 }, { a: 2, b: "x" }];
    expect(tableRows(rows)).toBe(rows);
  });

  it("rejects everything that is not a non-empty array of plain objects", () => {
    expect(tableRows([])).toBeNull();
    expect(tableRows(null)).toBeNull();
    expect(tableRows({ a: 1 })).toBeNull();
    expect(tableRows([1, 2])).toBeNull();
    expect(tableRows([null])).toBeNull();
    expect(tableRows([["nested"]])).toBeNull();
    expect(tableRows("not json")).toBeNull();
  });
});
