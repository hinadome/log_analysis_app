# SCRIPTS.md — writing analysis scripts, with per-language samples

LogSmith runs ordinary programs: your code, an interpreter, and a manifest
that tells the workbench how to invoke it. This file is the hands-on
companion to the README's
["How command, parameters and code fit together"](README.md#how-command-parameters-and-code-fit-together):
one small script — count log lines at a severity level, with a threshold,
writing a Markdown artifact — written in **all five supported languages**,
plus the rules that are identical for every one of them.

The shipped samples in `scripts/` are complete working references; the
table at the end maps each to what it demonstrates.

## The recipe (every language)

1. Drop two files into `scripts/`:
   - `scripts/<id>.<ext>` — the code
   - `scripts/<id>.json` — the manifest sidecar

   (A code file alone also works: `.py/.sh/.bash/.zsh/.js/.rb/.pl` get a
   default command `<interpreter> {script} {log} {args}` and no parameters.)

2. In the manifest, say how to run it and which flags exist:
   `"command": "<interpreter> {script} {log} {args}"` plus `parameters`.

3. In the code, read the input as the **first argument** and the parameters
   as flags after it.

4. Print to stdout (the job view shows it live); write files to the
   current directory to create artifacts.

At submission the template renders to **one argv, no shell involved**:

```
python3 /…/scripts/level_report.py /…/logs/app.log --level WARN --min-count 5
└ interpreter ┘└────── {script} ─────┘└── {log} ──┘└───────── {args} ─────────┘
```

Identical rules for every language:

| What your script gets | Value |
|---|---|
| Input path | first argument after the script — also `JOB_LOG_PATH` in the environment. Usually a file from `logs/`, but the user may submit any absolute path, so it can be a file elsewhere on the machine |
| Parameters | CLI flags spelled exactly as the manifest's `flag` fields |
| Booleans | the bare flag when true, **absent** when false (never `--flag false`) |
| Integers | always rendered, even when `0` |
| cwd | the job's `outputs/` directory — files written here become downloadable artifacts |
| Environment | `JOB_ID`, `JOB_LOG_PATH`, `JOB_OUTPUT_DIR`, `JOB_ORIGINAL_LOG` |
| Exit code | `0` → job `completed`; anything else → `failed` with the code shown |
| Timeout | `timeout` seconds from the manifest, then the process is killed |

Three things to keep in mind:

- **The interpreter must exist on the server.** Write `python3`, not
  `python` (fresh Linux boxes often only have the former), and
  `#!/usr/bin/env python3`-style shebangs for direct execution. A missing
  interpreter fails the job **at submission** with its name in the error
  — no guessing. The Docker sandbox image ships all five interpreters.
- **Save scripts with Unix LF line endings.** A Windows-style CRLF file
  passes every interpreter check yet dies at spawn with the cryptic
  `env: 'bash\r': No such file or directory` — the carriage return
  becomes part of the interpreter name, and of every shell command.
  LogSmith fails such a job **at submission** with the reason named
  (convert with `dos2unix`, or re-save the script in the UI editor,
  which always writes LF — even for text pasted from Windows tools).
- **No shell ever runs your command**, so parameter values are literal
  argv tokens: a value like `foo; rm -rf /` is just a string to grep for,
  and you never need extra quoting to survive interpolation.

## Python

The manifest (`scripts/level_report.json`) — full reference version:

```jsonc
{
  "name": "Level Report",
  "description": "Count lines at a severity level, with a threshold",
  "category": "counting",
  "command": "python3 {script} {log} {args}",
  "timeout": 120,
  "parameters": [
    {
      "name": "level", "flag": "--level", "label": "Severity",
      "type": "enum", "choices": ["ERROR", "WARN", "INFO"],
      "default": "WARN", "help": "Level token to count"
    },
    {
      "name": "min_count", "flag": "--min-count", "label": "Min matches",
      "type": "integer", "default": 0,
      "help": "Exit non-zero when there are fewer matches"
    }
  ]
}
```

`category` (optional) is a free-form short label — "web", "counting",
whatever groups your scripts. Both script pickers turn the distinct
values into filter chips, so a crowded list narrows to the group you are
working with. Scripts without a category simply have no chip.

The code (`scripts/level_report.py`) — argparse, the `error_counter.py`
style:

```python
#!/usr/bin/env python3
"""Count lines at a severity level and write a Markdown summary."""
import argparse
import os
import re
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("log", help="input: the selected log, or the previous chain step's output")
parser.add_argument("--level", default="WARN", help="severity token to count")
parser.add_argument("--min-count", type=int, default=0, help="report threshold")
args = parser.parse_args()

try:
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
except OSError as exc:
    print(f"cannot read {args.log}: {exc}", file=sys.stderr)
    sys.exit(2)

hits = [ln for ln in lines if re.search(rf"\b{re.escape(args.level)}\b", ln)]
enough = len(hits) >= args.min_count

# stdout: shown live in the job view
print(f"{args.log}: {len(hits)} lines at level {args.level} "
      f"({100 * len(hits) / max(len(lines), 1):.1f}% of {len(lines)})")
print("threshold:", "met" if enough else "NOT met")

# artifact: cwd is the job's outputs/ directory — report.md becomes downloadable
with open("report.md", "w") as out:
    out.write(f"# Level report\n\n"
              f"- log: `{os.environ.get('JOB_ORIGINAL_LOG', args.log)}`\n"
              f"- level: **{args.level}**\n- matching lines: **{len(hits)}**\n")

sys.exit(0 if enough else 1)
```

Renders to:

```
python3 /…/scripts/level_report.py /…/logs/app.log --level WARN --min-count 5
```

Notes: the positional is the input file; `type=int` coerces what the
manifest already validated; `sys.exit(2)` for usage errors, `1` for
"ran but the threshold failed", `0` for success.

## Bash (also `.bash` / `.zsh`)

Command: `bash {script} {log} {args}` (`.zsh` files default to the `zsh`
interpreter — same template shape). Manifest as above; the code
(`scripts/level_report.sh`), the `grep_search.sh` style:

```bash
#!/usr/bin/env bash
# Count lines at a severity level and write a Markdown summary.
set -eo pipefail

LOG=""
LEVEL="WARN"
MIN_COUNT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --level)      LEVEL="${2:-}"; shift 2 ;;
    --min-count)  MIN_COUNT="${2:-0}"; shift 2 ;;
    -*) echo "error: unknown option: $1" >&2; exit 2 ;;
    *)  LOG="$1"; shift ;;
  esac
done

[[ -f "$LOG" ]] || { echo "error: log file not found: $LOG" >&2; exit 2; }

# grep exits 1 on zero matches — catch it so set -e does not fail the script
count="$(grep -c -E "\b$LEVEL\b" "$LOG")" || rc=$?
count="${count:-0}"

echo "$LOG: $count lines at level $LEVEL"

# artifact: cwd is the job's outputs/ directory
{
  echo "# Level report"
  echo
  echo "- level: **$LEVEL**"
  echo "- matching lines: **$count**"
} > report.md

(( count >= MIN_COUNT ))
```

Notes: the log is `"$1"` — always quote it, paths may contain spaces.
Booleans arrive as bare flags (`--verbose`) or not at all — never test for
`= false`. The `|| rc=$?` pattern keeps `set -e` scripts alive through
grep's "no match" exit. A shebang is not required when the command names
the interpreter (`bash {script} …`); add one anyway if you might run the
script directly (below).

### Sourcing files (bash, zsh)

`source` works **inside your script's code** — bash is running it — but
a bare filename will not be found: the job's cwd is its `outputs/`
directory, not `scripts/`. Resolve helpers relative to the script
itself:

```bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/helpers.sh"     # config, env vars, shell functions…
```

What can never work: `source` in the manifest `command`. Commands are
executed without a shell and `source` is a shell builtin, not a program —
the job fails at submission with `'source' was not found`. Shell logic
that must run *before* your code belongs in the script (or in a wrapper
bash script that sources first, then does the work).

A side effect of discovery: a bare `.sh` file in `scripts/` shows up in
the UI as a runnable script of its own. To keep helper files out of the
run page, put them in a subdirectory such as `scripts/lib/` — discovery
ignores directories without a `manifest.json`, and your scripts can
still source them by path.

## Node.js

Command: `node {script} {log} {args}`. The code
(`scripts/level_report.js`), the `response_stats.js` style — note the
**argv offset**: `argv[0]` is `node`, `argv[1]` the script, so the input
file lands at `argv[2]`:

```js
#!/usr/bin/env node
// Count lines at a severity level and write a Markdown summary.
"use strict";
const fs = require("fs");

const logPath = process.argv[2];          // {log}
const args = process.argv.slice(3);       // {args}

function flag(name, fallback) {
  const i = args.indexOf(name);
  return i === -1 ? fallback : args[i + 1];
}
const level = flag("--level", "WARN");
const minCount = Number(flag("--min-count", 0));

if (!logPath) {
  console.error("usage: level_report.js <log> [--level ERROR] [--min-count N]");
  process.exit(2);
}

let lines;
try {
  lines = fs.readFileSync(logPath, "utf8").split("\n").filter(Boolean);
} catch (e) {
  console.error(`cannot read ${logPath}: ${e.message}`);
  process.exit(2);
}

const re = new RegExp(`\\b${level}\\b`);
const hits = lines.filter((l) => re.test(l));

console.log(`${logPath}: ${hits.length} lines at level ${level}`);
fs.writeFileSync("report.md",
  `# Level report\n\n- level: **${level}**\n- matching lines: **${hits.length}**\n`);

process.exit(hits.length >= minCount ? 0 : 1);
```

## Ruby

Command: `ruby {script} {log} {args}`. The code
(`scripts/level_report.rb`), the `hourly_rate.rb` style — the input file
is `ARGV[0]`:

```ruby
#!/usr/bin/env ruby
# Count lines at a severity level and write a Markdown summary.

level = "WARN"
min_count = 0
log = ARGV.shift

until ARGV.empty?
  case (arg = ARGV.shift)
  when "--level"     then level = ARGV.shift
  when "--min-count" then min_count = Integer(ARGV.shift, 10)
  else abort "error: unknown option: #{arg}"
  end
end

abort "usage: ruby level_report.rb <log> [--level ERROR] [--min-count N]" unless log
abort "cannot read #{log}" unless File.file?(log)

hits = File.readlines(log).grep(/\b#{Regexp.escape(level)}\b/)
puts "#{log}: #{hits.size} lines at level #{level}"

File.write("report.md",
  "# Level report\n\n- level: **#{level}**\n- matching lines: **#{hits.size}**\n")

exit(hits.size >= min_count ? 0 : 1)
```

## Perl

Command: `perl {script} {log} {args}`. The code
(`scripts/level_report.pl`), the `status_breakdown.pl` style — the input
file is `$ARGV[0]` (`Getopt::Long` from core works too):

```perl
#!/usr/bin/env perl
# Count lines at a severity level and write a Markdown summary.
use strict;
use warnings;

my ($level, $min_count) = ("WARN", 0);
my $log = shift @ARGV;

while (defined(my $arg = shift @ARGV)) {
    if    ($arg eq "--level")     { $level     = shift @ARGV; }
    elsif ($arg eq "--min-count") { $min_count = shift @ARGV; }
    else  { print STDERR "error: unknown option: $arg\n"; exit 2; }
}

die "usage: perl level_report.pl <log> [--level ERROR] [--min-count N]\n"
  unless defined $log;
open(my $fh, "<", $log) or die "cannot read $log: $!\n";
my @hits = grep { /\b\Q$level\E\b/ } <$fh>;
close $fh;

printf "%s: %d lines at level %s\n", $log, scalar @hits, $level;

open(my $out, ">", "report.md") or die "cannot write report.md: $!\n";
print $out "# Level report\n\n- level: **$level**\n",
           "- matching lines: **", scalar(@hits), "**\n";
close $out;

exit(@hits >= $min_count ? 0 : 1);
```

## Direct execution (any language)

Drop the interpreter from the command and make the code file executable
with a shebang — the script itself becomes argv[0]:

```
"command": "{script} {log} {args}"          # + chmod +x, + shebang line
```

```bash
./…/scripts/level_report.sh /…/logs/app.log --level WARN --min-count 5
```

Everything else — parameters, environment, artifacts, chains — is
unchanged. Scripts created in the UI editor are already written executable
(mode 755); hand-written files need `chmod +x`. The shebang must name an
interpreter that exists (`#!/usr/bin/env python3`, not `#!/usr/bin/python`
on a python3-only host) — otherwise the job fails at submission with the
shebang quoted in the error.

## Artifacts, two ways

1. **Write to the current directory** — all samples above do this: cwd is
   the job's `outputs/` directory, so `report.md` lands there
   (`$JOB_OUTPUT_DIR` holds the absolute path) and appears in the job's
   Files tab — rendered in the browser by the react UI, offered as a
   download by the python UI.

2. **The `{output}` placeholder** — when the manifest sets
   `output_filename` and the command includes `{output}`, the server
   passes the artifact's full path as an argument instead:

   ```
   "command": "python3 {script} {log} {output} {args}",
   "output_filename": "report.md"
   ```

   ```python
   parser.add_argument("--out")   # the {output} slot
   # …
   with open(args.out, "w") as fh: …
   ```

## Chains

On the run page, switch the mode from parallel to **chain** (the
⚡ Parallel / 🔗 Chain toggle in the react UI, the parallel/chain radio in
the python UI). The steps then run **in order**, and each step's input
slot — the `{log}` placeholder — receives the **previous step's output**
instead of the selected log. Everything else about a step is unchanged:
same parameters, same artifacts, same exit-code rules.

Both examples below were run for real against the sample data.

### Example 1 — filter, then count (app.log)

Steps: `grep_search --pattern timeout` → `error_counter --level ERROR`.

Step 1 keeps the lines containing "timeout"; step 2 counts ERROR lines
— but only among what step 1 kept. Step 2's rendered command, from the
live run:

```
python3 …/scripts/error_counter.py …/data/jobs/<step-1>/stdout.txt --level ERROR
                                    └── the previous step's output, in the {log} slot
```

Step 1 printed 5 matching lines (all ERROR) plus its own trailer line
(`-- 5 matching line(s) for pattern 'timeout' --`), so error_counter
reported **7 total lines, 5 at ERROR** — one data point worth internalizing:
when a step has no artifact, its **entire stdout** is chained, trailers
and progress lines included.

### Example 2 — filter, then report (access.log)

Steps: `grep_search --pattern " 500 "` → `access_report --top 3`.

The second step is an `{output}` script (its command is
`python3 {script} {log} --output {output}`), so both placeholders
resolve in the same render — `{log}` is the chained input, `{output}`
is this step's own artifact:

```
python3 …/scripts/access_report.py …/data/jobs/<step-1>/stdout.txt --output …/data/jobs/<step-2>/outputs/report.md --top 3
```

The artifact it produced (live run): *Requests analyzed: 4 · 5xx server
error: 4 · `500`: 4* — a Markdown report built from just the 500
responses.

### What gets passed to the next step

A completed step hands its successor one file, chosen in this order:

| Order | The step passes on … | Condition |
|---|---|---|
| 1 | its declared artifact | the command has `{output}` and the file exists |
| 2 | the one file it wrote into `outputs/` | exactly one file was written |
| 3 | everything it printed to stdout | no artifact at all |

So if a step's payload should chain cleanly, write it as a file —
tiers 1 and 2 — rather than printing it mixed with progress chatter.
A step that produces nothing at all fails the next one with
*"Previous step produced no output to chain"*.

Compressed intermediates are handled for you: if a step's output is a
`.gz`/`.bz2`/`.xz` file, it is decompressed before the next step reads it.

### What each step sees

- **argv / `$1` / `JOB_LOG_PATH`**: the previous step's output (the
  selected log for step 1)
- **`JOB_ORIGINAL_LOG`**: the log the submission started from — every
  step can still see where the data came from
- **`JOB_OUTPUT_DIR` / cwd**: its own `outputs/` directory, as usual

One distinction worth spelling out: the hand-off is a **file path in
argv — nothing is ever piped into the script's stdin** (it is not
connected to the chain; `select` on it gets immediate EOF). Your script
opens the input as a file and reads it, which is what every sample in
this document does.

Each step is a normal job — its own ID, page, stdout, artifacts — just
waiting instead of queued until its predecessor finishes. A failed or
cancelled step marks the remaining steps `skipped`; cancelling any step
cancels the rest. A single-step chain behaves like a plain run.

### Chains work for free

A script that reads its input argument needs **zero changes** to join a
chain: the previous step's output arrives in the same slot — `$1` /
`sys.argv[1]` / `process.argv[2]` / `ARGV[0]` / `$ARGV[0]` — and in
`JOB_LOG_PATH`. Any two scripts in this file compose: `grep_search` →
`level_report --min-count 5` is a threshold alert over the filtered
stream, with no new code.

### Running chains

- **UI**: pick the log, add the scripts as steps (numbered in chain
  mode), fill parameters per step, submit. The react UI can also save a
  pipeline (script + parameters per step) to reuse later; every job page
  shows the whole chain as a step strip, and a chain can be re-run in
  full or just from a given step onward.
- **API**: the same submission body with `mode` flipped:

```json
{
  "log_path": "app.log",
  "mode": "chain",
  "jobs": [
    {"script_id": "grep_search", "params": {"pattern": "timeout"}},
    {"script_id": "error_counter", "params": {"level": "ERROR"}}
  ]
}
```

Steps execute in array order.

## The shipped samples

| Sample | Demonstrates |
|---|---|
| `error_counter.py` | argparse with positional + enum/string/boolean parameters, JSON output mode |
| `grep_search.sh` | required string parameter, boolean and integer flags, grep's exit-code handling |
| `response_stats.js` | Node argv offsets, flag helpers, humanized byte sizes |
| `hourly_rate.rb` | Ruby option parsing, bucketed histogram with a bar chart |
| `status_breakdown.pl` | Perl option parsing, aggregation into hashes |
| `top_ips.py` | JSON array-of-objects output — renders as a sortable table + CSV in the UI |
| `access_report.py` | the `{output}` placeholder — a Markdown report written to the artifact path |
| `slow_scan.py` | long-running job with progress lines (live tail), an artifact, an intentional-fail parameter |

Drop any of them on a log from the Run page, or open them in the editor
(✏️) to see manifest and code side by side.
