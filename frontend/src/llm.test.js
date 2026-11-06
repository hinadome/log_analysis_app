// The LLM settings panel's pure helpers: key masking hints, client-side
// validation mirroring the server's shape checks, and the /test result
// rendered as one line.
import { describe, expect, it } from "vitest";

import { keyPlaceholder, testMessage, validateSettings } from "./llm.js";

describe("keyPlaceholder", () => {
  it("says when no key is stored", () => {
    expect(keyPlaceholder("", false)).toBe("not set");
  });

  it("shows the mask with the keep hint", () => {
    expect(keyPlaceholder("sk-…3456", true)).toBe("sk-…3456 saved — leave blank to keep");
  });
});

describe("validateSettings", () => {
  it("accepts empty base urls (the registry default applies)", () => {
    expect(validateSettings({ openai: { base_url: "" } })).toBeNull();
  });

  it("accepts http and https base urls", () => {
    expect(validateSettings({
      openai: { base_url: "https://api.openai.com/v1" },
      ollama: { base_url: "http://localhost:11434/v1" },
    })).toBeNull();
  });

  it("rejects other schemes with one shared reason", () => {
    expect(validateSettings({
      "openai-compatible": { base_url: "ftp://nope" },
    })).toBe("Base URLs must start with http:// or https://");
  });
});

describe("testMessage", () => {
  it("formats a success with the reply and latency", () => {
    expect(testMessage({ ok: true, reply: "ok", elapsed_ms: 412 }))
      .toBe("Provider replied in 412 ms: \u201Cok\u201D");
  });

  it("passes the failure reason through", () => {
    expect(testMessage({ ok: false, error: "OpenAI answered HTTP 401" }))
      .toBe("OpenAI answered HTTP 401");
  });

  it("handles a missing result and a bare failure", () => {
    expect(testMessage(null)).toBeNull();
    expect(testMessage({ ok: false })).toBe("The provider test failed");
  });
});
