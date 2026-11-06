/**
 * Pure helpers for the LLM settings panel (no React, no fetch — the
 * command.js pattern: unit-testable logic, thin component on top).
 */

/**
 * Placeholder for the api-key input: enough of the saved key to
 * recognize it (the server masks it), never enough to use it. Blank
 * input keeps the stored key — that's enforced server-side.
 */
export function keyPlaceholder(masked, hasKey) {
  if (!hasKey) return "not set";
  return `${masked} saved — leave blank to keep`;
}

/**
 * Client-side mirror of the server's shape checks (the server stays
 * authoritative — this is instant feedback only). Returns an error
 * message or null.
 */
export function validateSettings(fields) {
  for (const f of Object.values(fields || {})) {
    const base = (f.base_url || "").trim();
    if (base && !/^https?:\/\//.test(base)) {
      return "Base URLs must start with http:// or https://";
    }
  }
  return null;
}

/**
 * The /api/llm/test result as one line: what the provider answered and
 * how long it took, or why it didn't.
 */
export function testMessage(result) {
  if (!result) return null;
  if (result.ok) {
    return `Provider replied in ${result.elapsed_ms} ms: “${result.reply}”`;
  }
  return result.error || "The provider test failed";
}
