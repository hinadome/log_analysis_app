// The editor's target-file choice: {log} in the command, out of it, or
// the extension default — the logic behind the radio pair.
import { describe, expect, it } from "vitest";

import { commandNeedsLog, defaultCommand, ensureLog, stripLog } from "./command.js";

describe("defaultCommand", () => {
  it("builds the interpreter invocation per extension", () => {
    expect(defaultCommand("py")).toBe("python3 {script} {log} {args}");
    expect(defaultCommand("sh")).toBe("bash {script} {log} {args}");
    expect(defaultCommand("rb")).toBe("ruby {script} {log} {args}");
  });

  it("falls back to direct execution for an unknown extension", () => {
    expect(defaultCommand("exe")).toBe("{script} {log} {args}");
  });
});

describe("commandNeedsLog", () => {
  it("reads the log while {log} is in the command", () => {
    expect(commandNeedsLog("python3 {script} {log} {args}")).toBe(true);
    expect(commandNeedsLog("python3 {script} {args}")).toBe(false);
  });

  it("treats a blank command as the default, which reads the log", () => {
    expect(commandNeedsLog("")).toBe(true);
    expect(commandNeedsLog("   ")).toBe(true);
  });
});

describe("stripLog", () => {
  it("removes {log} from the command", () => {
    expect(stripLog("python3 {script} {log} {args}", "py"))
      .toBe("python3 {script} {args}");
  });

  it("takes a flag {log} was glued to with it", () => {
    expect(stripLog("bash {script} --input={log} {args}", "sh"))
      .toBe("bash {script} {args}");
  });

  it("starts from the extension default when the command is blank", () => {
    expect(stripLog("", "py")).toBe("python3 {script} {args}");
  });

  it("falls back to {script} {args} when nothing survives", () => {
    expect(stripLog("{log}", "py")).toBe("{script} {args}");
  });
});

describe("ensureLog", () => {
  it("inserts {log} before {args}", () => {
    expect(ensureLog("python3 {script} {args}"))
      .toBe("python3 {script} {log} {args}");
  });

  it("appends {log} when there is no {args}", () => {
    expect(ensureLog("python3 {script}")).toBe("python3 {script} {log}");
  });

  it("keeps a command that already has it", () => {
    expect(ensureLog("python3 {script} {log} {args}"))
      .toBe("python3 {script} {log} {args}");
  });

  it("keeps a blank command — the default already reads the log", () => {
    expect(ensureLog("")).toBe("");
  });
});
