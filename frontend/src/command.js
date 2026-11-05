// The editor's target-file choice as command-template edits: a script
// reads the selected log while its command keeps {log}; "none" strips it,
// making the script log-free (runnable with nothing selected).

export const INTERPRETERS = {
  py: "python3", sh: "bash", bash: "bash", zsh: "zsh",
  js: "node", rb: "ruby", pl: "perl",
};

/** The command a manifest gets when its own is blank (mirrors the server). */
export function defaultCommand(extension) {
  const interpreter = INTERPRETERS[extension];
  return interpreter ? `${interpreter} {script} {log} {args}` : "{script} {log} {args}";
}

/** A command reads the log while it holds {log}; a blank one means the
 *  extension default, which reads it too. */
export function commandNeedsLog(command) {
  return !command.trim() || command.includes("{log}");
}

/** Target file: none — {log} out of the command (a flag it was glued to
 *  goes with it). A blank command starts from the extension default. */
export function stripLog(command, extension) {
  const base = command.trim() || defaultCommand(extension);
  const tokens = base
    .split(/\s+/)
    .map((t) => t.replaceAll("{log}", ""))
    .filter((t) => t && !t.endsWith("="));
  return tokens.join(" ") || "{script} {args}";
}

/** Target file: selected log — make sure {log} is in the command. Blank
 *  stays blank: the default already reads the log. */
export function ensureLog(command) {
  const base = command.trim();
  if (!base || base.includes("{log}")) return base;
  if (base.includes("{args}")) return base.replace("{args}", "{log} {args}");
  return `${base} {log}`;
}
