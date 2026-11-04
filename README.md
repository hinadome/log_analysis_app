# LogSmith — Log Analysis Workbench

A web application for running server-side analysis scripts against server-side
log files. Pick a log, pick one or more scripts, set parameters, run — jobs
execute asynchronously with live output streaming, and results (including
artifact files) are viewable in the browser.

**Docs:** [SPECS.md](SPECS.md) — normative functional/API spec ·
[ARCHITECTURE.md](ARCHITECTURE.md) — system design and chain-engine internals ·
[SCRIPTS.md](SCRIPTS.md) — writing analysis scripts, with per-language samples

## Quick start

Prerequisites: Python 3.10+ — plus Node.js for the default react UI
(`--ui python` needs no Node at all). Everything else the first
`./run.sh start` does by itself:

```bash
# 1. Sample data (skippable if you point LOGS_DIR/SCRIPTS_DIR at your own dirs)
python3 tools/generate_sample_logs.py

# 2. Production-style: backend serves the built SPA
./run.sh start                                      # https://localhost:8000

# 3b. Or dev mode with hot reload (frontend on :5173, proxies /api)
./run.sh start --dev                                # backend + Vite together

# 3c. Or skip Node entirely: server-rendered pages, no build step
./run.sh start --ui python                          # same workbench, plain HTML

# Control (plain userland — no systemctl/root needed)
./run.sh stop                                       # stop everything it started
./run.sh status                                     # what's running
./run.sh restart [--dev] [--ui python|react]        # stop + start
./run.sh                                            # foreground server (Ctrl+C stops)
```

First start sets itself up: `.venv` and its python packages, `node_modules`
and the SPA build are created on demand — and reinstalled automatically when
`backend/requirements.txt` or `frontend/package-lock.json` changed.

`--ui` (or `LOGSMITH_UI`) picks the frontend: `react` — the default SPA
built with Vite — or `python` — Jinja2 pages rendered by the backend with
zero build tooling. Both serve the same `/api` and the same session
cookie, so either can be pointed at the same data. See
[ARCHITECTURE.md §8](ARCHITECTURE.md#8-frontend-architecture) for the
differences (short version: parameter presets and notifications are
React-only; the python UI trades them for deep-linkable URLs and no
Node dependency).

`start` runs in the background: logs append to `.run/*.log`, PIDs live in
`.run/*.pid` (gitignored), and `stop` only kills processes this script
started. In dev mode the backend auto-reloads on backend-code changes
(running jobs are cancelled on reload) and Vite proxies `/api` to it.

## Copying to another machine

`tools/upload.sh` scps the app to a remote host — exactly the git-tracked
files, so `.venv`, `node_modules`, `frontend/dist`, `data/`, `certs/`,
`.run/` and `.env` stay behind (`.gitignore` by construction; no secrets
or job history leave the machine). The remote then bootstraps itself on
its first `./run.sh start`:

```bash
tools/upload.sh user@192.168.1.50            # → ~/logsmith on the remote
tools/upload.sh -p 2222 me@server:apps/logsmith   # custom ssh port + dir
tools/upload.sh --start user@host           # unpack and (re)start it there
tools/upload.sh --tar-only                   # just build the tarball, no upload
```

Re-uploads refresh the tracked files in place — remote state (jobs, token,
certs) is untouched. Target dirs may be home-relative, `~/…`-style or
absolute (the remote shell expands the tilde). `--start` restarts the app
there, so the uploaded code goes live. A dirty working tree is refused
unless `--dirty` is passed (snapshots uncommitted tracked changes; new
untracked files — e.g. scripts created in the UI editor — never upload
without a commit; the refusal lists exactly what is uncommitted).

The remote bootstraps itself on its first `./run.sh start` (`.venv`,
packages, `node_modules`, SPA build — all on demand). It needs python3;
Node is only required for the react UI, so `--ui python` runs without it.
Normative behavior: [SPECS.md §10](SPECS.md#10-deployment-toolsuploadsh).

## HTTPS

`run.sh` serves the app over **https://localhost:8000**. On first start it
generates a local certificate into `certs/` (via `tools/generate_cert.sh`) and
reuses it on every later start — it is never regenerated while it exists.
Dev mode (`./run.sh start --dev`) uses the same certificate, because the
Vite proxy talks HTTPS to the backend.

- **With [mkcert](https://github.com/FiloSottile/mkcert) installed**
  (`brew install mkcert`): the certificate is trusted by your browser, no
  warnings. Delete `certs/` and restart to upgrade an existing self-signed
  cert to a trusted one.
- **Without mkcert**: a self-signed certificate (SANs: `localhost`,
  `127.0.0.1`, `::1`, valid 825 days) is generated with openssl. Browsers show
  a one-time "not private" warning — proceed or install mkcert.
- **From other devices** (the server binds all interfaces by default): the
  certificate only covers `localhost`, so other machines see a browser
  warning. To include them, regenerate it for their address:

  ```bash
  ./run.sh restart --cert-hostname '192.168.1.50,my-mac.local'
  ```

  The option explicitly replaces the existing certificate (the `localhost`
  SANs stay included; hostnames become DNS SANs, dotted quads become IP
  SANs) — the only way to regenerate one, since a certificate is otherwise
  kept forever once generated. The `LOGSMITH_CERT_HOSTS` environment
  variable adds the same names when a certificate is first generated.
- Plain HTTP is disabled when HTTPS is on. Set `LOGSMITH_HTTP=1 ./run.sh` to
  fall back to HTTP.
- Running uvicorn manually with TLS:

  ```bash
  .venv/bin/uvicorn --app-dir backend app.main:app \
    --ssl-certfile certs/localhost.pem --ssl-keyfile certs/localhost-key.pem
  ```

- Frontend dev mode already proxies to the HTTPS backend (`secure: false`
  accepts the self-signed cert).
- `certs/` is git-ignored; never commit private keys.


## Features

- **Two frontends, one app** — the default React SPA, or `--ui python`:
  server-rendered pages with the same workbench (logs, runs, job history,
  compare, the script editor) and no build step. Every view is a plain
  deep-linkable URL; the two are mutually exclusive at startup because the
  SPA owns a catch-all route.
- **Async execution** — jobs get an ID immediately and run in a bounded pool
  (`MAX_CONCURRENT_JOBS`, default 4), overlapping in wall-clock. Extra jobs
  queue without limit (first-come-first-served, starting as slots free).
  The server stays fully responsive while jobs run — execution is async
  end-to-end (subprocesses with awaited I/O), and that is pinned by a
  regression test.
- **Self-diagnosing failures** — a job that cannot start says exactly why:
  a first token that is not executable, a missing shebang, or a missing
  interpreter — including the one named in a script's shebang
  (`#!/usr/bin/python` on a python3-only host reads like a lost file, but
  is a missing interpreter). Commands are pre-flighted at submission:
  a missing interpreter fails the job instantly with the culprit named
  instead of erroring later, and the rest of a chain is skipped.
- **Deep links** — every job has a URL (`#/job/<id>`); refresh, the browser
  back button and shared links all land on the right view.
- **Live streaming** — the job detail page uses server-sent events
  (`GET /api/jobs/{id}/events`) for zero-latency stdout while a script runs,
  with automatic fallback to polling.
- **Remote terminal** — `LOGSMITH_TERMINAL=1` adds a Terminal tab: a real
  interactive shell on the server in the browser (xterm.js over SSE + POST,
  no WebSocket, no extra dependency). Off by default — a shell is the whole
  machine. Both UIs have it: React's Terminal tab, and the python UI's
  `/terminal` page, where the same shell is progressive enhancement — with
  JavaScript it mounts (xterm.js vendored into the repo, no Node needed),
  without it the page's command box stands in. Off also means *hidden*:
  every terminal endpoint answers 404.
- **File artifacts** — anything a script writes to its `outputs/` directory is
  viewable/downloadable in the browser; Markdown files render nicely.
- **Authentication** — `LOGSMITH_TOKEN` requires a password for the UI
  (browser login screen, HttpOnly session cookie). Automation can send
  `Authorization: Bearer <token>`. The server listens on **all interfaces**
  by default, and scripts run as your user — so the launcher is secure by
  default: binding a non-local address with no token set generates a strong
  password (printed once, stored in `.run/authtoken`, reused across
  restarts). `LOGSMITH_INSECURE=1` keeps it open, `LOGSMITH_HOST=127.0.0.1`
  makes it local-only with no auth.
- **Script authoring lock** — scripts are code that runs on the server, so
  the editor is remote code execution by design (trusted-operator tool).
  When other people have the password, `LOGSMITH_ALLOW_SCRIPT_EDITING=0`
  locks it down: creating, changing or deleting scripts answers 403 in both
  UIs while the scripts on disk stay runnable — "run the vetted analyses
  only".
- **Docker sandbox** — `LOGSMITH_DOCKER_SANDBOX=1` runs every script inside
  a throwaway container instead of directly on the host: no network, a
  memory cap, and only the script, its input (read-only) and the output
  directory mounted. The sandbox image (python3, bash, node, ruby, perl) is
  built automatically from `sandbox.Dockerfile` on first start; extend that
  image and point `LOGSMITH_SANDBOX_IMAGE` at your tag when scripts need
  extra libraries.
- **gzip logs** — `.gz`, `.bz2` and `.xz` logs are transparently decompressed
  before scripts run, and preview correctly in the UI. The decompressed copy
  is **shared**: one per unique source file (under `data/cache/decompressed/`)
  no matter how many scripts read it, so a fan-out on one `.gz` costs a
  single decompression instead of one copy per job. Deleting the log (or
  the 🧹 Prune button) reclaims its copy.
- **Log-free runs** — the input log is optional. A script whose command has
  no `{log}` placeholder (marked *log-free* in both UIs — the shipped
  `disk_report` system snapshot is one) runs and schedules without a
  selected log; its process simply gets no `JOB_LOG_PATH`/`JOB_ORIGINAL_LOG`
  and the sandbox mounts no input. A script that *does* read `{log}`
  submitted without one is rejected with a 422 naming it — and in a chain
  only step 1 is checked, since the rest read each other's output.
- **Job retention** — `JOB_RETENTION_DAYS` auto-deletes old jobs at startup
  (0 = off); the Jobs panel has a manual 🧹 Prune button and shows storage use.
- **Chain mode (pipelines)** — submit the same scripts as an ordered chain:
  steps run sequentially and each step's `{log}` is the previous step's output
  (its single artifact file, or its stdout when no artifact was produced).
  A failed step skips the rest; cancelling any step cancels the chain; re-running
  any step re-runs the whole chain. `JOB_ORIGINAL_LOG` always points at the
  original file. Scripts need **zero changes** to work in chains.
  The order is visible and editable in both UIs: React numbers the step cards
  with ↑/↓ reorder buttons; the python UI numbers the parameter fieldsets
  (`Step N`) with ↑/↓ links, and its script-table checkboxes merge into the
  carried order on Update — so a reordered chain survives an Update instead
  of snapping back to the table's alphabetical order.
- **Continue-from-step-N** — re-run only from a chosen step onwards: earlier
  steps' persisted outputs are reused, so iterating on step 3 doesn't pay for
  step 1 again ("Re-run from here" on any chain step).
- **Step-input viewer** — the Input tab shows exactly what a script received:
  the selected log, or for chain steps the previous step's actual output
  (compressed sources shown decompressed).
- **Saved pipelines** — save a whole ordered chain (scripts + parameters)
  under a name and load it on any log, like parameter presets but for
  chains. Stored on the server (`data/pipelines.json`), so every browser
  and both UIs share the same list: React saves/loads/deletes via the
  runbar; the python UI has a "Saved pipelines" card on the run page with
  Load and 🗑 Delete buttons, plus a 💾 button in the runbar. A loaded
  pipeline is a ready-to-run, fully configured chain.
- **Scheduled runs (cron)** — a schedule is a saved pipeline **or a single
  script** + a log + a 5-field cron expression (`0 8 * * *` = daily at
  08:00, server-local time). An in-process ticker fires it through the
  normal submission path, so scheduled runs are ordinary jobs (labelled
  with the schedule's name) — history, retention and the concurrency cap
  apply unchanged. A pipeline reference is resolved fresh at every fire
  (edit the pipeline, every schedule pointing at it updates); a single
  script is stored as the schedule's own validated steps. Overlapping runs
  are skipped (visibly recorded, never queued behind each other), and a
  fire missed while the server was down stays missed — no catch-up, no
  retries, no backfill. Each schedule shows next fire, last run/result,
  with run-now, pause/resume and delete in both UIs; preset chips build
  common expressions.
- **Fan-out over logs** — arm extra logs with the ＋ toggle in the log list
  and one Run submits the same scripts against each of them ("3 logs × 2
  steps = 6 jobs"), e.g. the same counter across all rotated files.
- **Any file by path** — pick from `logs/`, or type an absolute path
  (React: the box under the log filter; python UI: "Use a file by path")
  and analyze any existing file on the machine LogSmith runs on —
  `/var/log/system.log`, a big export, whatever. Previews work on it too,
  and jobs from outside `logs/` carry the full path as their name. Scripts
  run as your user with full filesystem access anyway, so this opens no
  new doors.
- **Long run configs stay manageable** — every parameter card folds down
  to its title bar (chain reorder controls stay put), with Collapse all /
  Expand all in the section header; loading a saved pipeline starts
  collapsed since it is already configured. The Run button and the
  `logs × steps = jobs` math stay pinned to the bottom of the viewport
  while the stack scrolls — a sticky runbar in both UIs.
- **Script categories** — give scripts a `category` in their manifest
  ("web", "counting", …) and the script pickers grow filter chips that
  narrow the list to one group: React filters instantly client-side, the
  python UI via `?cat=` links that rebuild the full run configuration.
  Chosen scripts stay chosen even when their category is filtered out of
  view. The shipped samples are categorized (`web`, `counting`,
  `search`, `testing`), and the editor has a Category field in both UIs.
- **Labels & job search** — tag a submission with a label ("timeout spike
  investigation") at creation time and filter the job history by label,
  script, log, chain or free text — searched server-side (debounced) and
  paged, so the history stays responsive as it grows. Status chips
  (Active / Completed / Failed / Cancelled) filter the same way.
- **Log upload** — drag & drop log files straight into the browser (or
  click to browse; the python UI has a plain upload form); they land in the
  server's `logs/` directory ready to analyze. Collisions are rejected,
  sizes are capped (`MAX_LOG_UPLOAD_MB`), and partial uploads never show
  up half-written.
- **Head & tail previews** — preview a log from the top or the bottom
  (⇣ Tail is usually what you want); tail seeks backwards from EOF, so it
  is instant even on huge files.
- **Table & diff views** — stdout that is a JSON array of objects renders
  as a sortable table (click a header: ascending → descending → off) with a
  CSV download of what's on screen; any finished job can be diffed against
  another with "⇄ Compare" (unified line diff, before/after at a glance).
- **Script editor** — create, edit, duplicate and delete scripts in the
  browser: code textarea, manifest form (name, command, timeout, output file)
  and a JSON parameter editor. Writes ordinary flat-style files into
  `scripts/`, so hand-added scripts and UI-created ones are interchangeable.
  One-click **backup** (tar.gz download) and code copy make deletes
  non-scary, and **Import** brings a backup back — or moves scripts between
  servers (a pack of flat scripts in one archive works too). The bundled
  samples cover every editor language — Python, Bash (`.sh`/`.bash`/`.zsh`),
  Node, Ruby and Perl — and double as reference templates.
- **Test-run from the editor** — ▶ Run… saves the script (even unsaved
  edits), runs it with its default parameters on a log you pick, and jumps
  straight to the live result.
- **Brute-force protection** — failed logins are rate-limited per client
  (5 per minute) once `LOGSMITH_TOKEN` is set.
- **Notifications** *(React UI)* — enable the 🔔 button in the top bar to
  get a browser notification when a job finishes (the python UI refreshes
  the job page instead).
- **Parameter presets** *(React UI)* — save/load named parameter combos per
  script (stored in your browser's localStorage).
- **Test suite** — `backend/tests/` (pytest) locks in the chain invariants,
  the API contract and the storage layer; `frontend/` (vitest) locks in the
  diff and table logic. See
  [ARCHITECTURE.md §11](ARCHITECTURE.md#11-testing).

## How it works

```
┌────────────┐   1. list      ┌──────────────────────┐
│   logs/    │◄──────────────┤                      │   5 scripts may write
│  (server)  │──────────────►│      FastAPI         │     artifacts into
└────────────┘   preview     │   /api/*             │     outputs/<job>/
┌────────────┐                │                      │            │
│  scripts/  │◄── 2. discover ┤  JobStore (JSON)     │            ▼
│ +manifests │─── manifests ──►  Executor (async     │   ┌──────────────┐
└────────────┘                │   subprocess, pool)  │──►│ data/jobs/   │
        ▲                     └──────────────────────┘   │ stdout.txt   │
        │ 3. POST /api/jobs  {log, mode, jobs}          │ stderr.txt   │
        └──────────────┐                                │ outputs/…    │
                       ▼                                └──────────────┘
              React UI (Vite) / python pages ◄── 4. poll GET /api/jobs/{id} (live tail + SSE)
```

In **chain** mode the executor runs the steps sequentially and hands each
completed step's output to the next one — see
[ARCHITECTURE.md §4](ARCHITECTURE.md#4-chain-pipeline-engine) for the
engine internals.

- **Logs**: every file under `logs/` (recursive) is selectable, with a
  preview modal before you commit.
- **Scripts**: every script under `scripts/` with a manifest is selectable.
  Steps form an ordered list — run them in **parallel** (each reads the
  selected log) or as a **chain** (each step feeds the next).
- **Async execution**: jobs get an ID immediately and run in a bounded pool
  (`MAX_CONCURRENT_JOBS`, default 4). The UI polls and live-tails stdout.
- **Results**: stdout/stderr are kept per job; anything a script writes to its
  `outputs/` directory is downloadable/viewable in the browser.

## Script manifest reference

Ready-to-adapt samples of the same script in all five languages (python,
bash, node, ruby, perl) live in [SCRIPTS.md](SCRIPTS.md); this section is
the full reference. Scripts are discovered in two shapes:

1. **Flat**: `scripts/name.py` + sidecar `scripts/name.json`
2. **Directory**: `scripts/name/manifest.json` + entry file (named via `entry`,
   or auto-detected)

Without a manifest, `.py/.sh/.js/.rb/.pl` files get a sensible default command.

You don't have to write these files by hand: the **＋ New script** button in
the UI opens a browser editor for the flat style — code, manifest fields and
parameters — and existing scripts can be edited (✏️ on any script card) or
deleted there too. UI-created scripts are indistinguishable from hand-added
ones on disk, except that the editor writes the code file **executable**
(mode 755) — so `command` may also run the script directly (`{script}
{log} {args}`) when the code starts with a shebang line like `#!/bin/bash`.
Scripts written by hand need `chmod +x` for that; run through an
interpreter (`bash {script} {log}`) they need neither.

The `{log}` argument is always the plain, uncompressed input: `.gz`/`.bz2`/
`.xz` logs (and compressed outputs of a previous chain step) are
decompressed before your script runs, so no script ever needs to handle
compression.

```jsonc
{
  "name": "Error Counter",              // shown in the UI
  "description": "Count lines by level",
  "command": "python3 {script} {log} {args}",
  "timeout": 120,                       // seconds, job is killed after
  "output_filename": "report.md",        // name for the {output} placeholder
  "parameters": [
    {
      "name": "level",                  // key the UI/API uses
      "flag": "--level",                // CLI flag rendered into {args}
      "label": "Severity",              // friendlier UI label
      "type": "enum",                   // string|integer|float|boolean|enum|multi
      "choices": ["ERROR", "WARN"],    // enum only
      "default": "ERROR",
      "required": false,
      "help": "Level to count"
    }
  ]
}
```

### How command, parameters and code fit together

A script is three pieces — the **code file**, the manifest's **`command`**
template and its **`parameters`** — and they meet in exactly one place:
the command line the server renders at submission. Everything the manifest
declares ends up on that command line or in the environment; everything the
code parses must come from those two.

Worked example — the bundled `error_counter` sample, a user picks `app.log`,
severity `WARN`, filter `timeout`, JSON output on:

```
 manifest (error_counter.json)                UI form (built from "parameters")
 ┌───────────────────────────────────┐        ┌─────────────────────────────────┐
 │ "command":                        │        │ Severity   (level)   [WARN ▾]   │
 │    python3 {script} {log} {args}  │        │ Filter     (pattern) [timeout ] │
 │ "parameters": [                    │        │ JSON output (json)   ☑         │
 │   level   --level   enum           │        └────────────────┬────────────────┘
 │   pattern --pattern string         │                         │  params object
 │   json    --json    boolean        │                         ▼
 │ ]                                  │    {"level":"WARN","pattern":"timeout",
 └───────────────────────────────────┘     "json":true}
                                                │  rendered at submission
                                                ▼
 python3 /…/scripts/error_counter.py /…/logs/app.log --level WARN --pattern timeout --json
 └ interpreter ┘└──────── {script} ─────────┘└─ {log} ─┘└──────────── {args} ─────────────┘

 the code (error_counter.py) receives exactly:
   argv : ["…/error_counter.py", "…/app.log", "--level", "WARN",
           "--pattern", "timeout", "--json"]
   env  : JOB_ID, JOB_LOG_PATH, JOB_OUTPUT_DIR, JOB_ORIGINAL_LOG
   cwd  : the job's outputs/ directory
```

Step by step:

1. **`command` positions everything.** At submission each placeholder is
   substituted (table below); the template is tokenized with shell rules
   but executed **without a shell**, so a parameter value can never be
   re-read as shell syntax. A template without `{args}` is a *fixed*
   command — parameters are still validated, but no flags are appended.

2. **`parameters[].flag` is the contract with the code.** `{args}` is
   built from the filled-in values by type: `boolean` true renders as the
   bare flag (`--json`), `multi` repeats the flag per value
   (`--field a --field b`), everything else as `--flag value`; empty/false
   values are omitted. Those flag strings are exactly what the script must
   parse — in the sample every parameter has a matching
   `parser.add_argument("--level", …)`. The `name` is only the key used by
   the UI and API (`"params": {"level": "WARN"}`); it never appears on the
   command line.

3. **The code reads argv and/or the environment.** With the default
   template the input path is the first argument (`$1` / `sys.argv[1]` /
   …), and `JOB_LOG_PATH` carries it even for commands that omit `{log}`
   — see "Reading the input in your script" below. In a chain, the same
   slots carry the previous step's output instead of the selected log.
   A command without `{log}` at all is a *log-free* script: it runs with
   no selected log, and its process gets no `JOB_LOG_PATH`
   ([SCRIPTS.md](SCRIPTS.md)).

The rendered command is stored on the job, so you can always verify what
actually ran: both UIs show it in the job's details view ("Executed
command (argv, no shell)" in React, the details tab in the python UI).

Direct-execution variant: with a shebang in the code
(`#!/usr/bin/env python3`) and command `{script} {log} {args}`, the script
file itself is argv[0] — same placeholders, same parameter contract, one
fewer file in the command line.

This same script written out in every supported language — python, bash,
node, ruby and perl — is in [SCRIPTS.md](SCRIPTS.md).

### Command placeholders

| Placeholder | Expands to |
|---|---|
| `{script}` | absolute path of the script |
| `{log}` | absolute path of the selected log — in chain mode, the **previous step's output** for steps 2+. Optional: a command without it makes the script **log-free** (runs with no log selected — see [SCRIPTS.md](SCRIPTS.md)) |
| `{output}` | `<job>/outputs/<output_filename>` — file artifacts |
| `{args}` | rendered CLI flags from validated parameters |

### Reading the input in your script

`{log}` is a placeholder of the **command template**, not of your code: at
submission it becomes the absolute input path and your script receives it
as a command-line argument — with the default commands, the first one.

```bash
LOG="$1"                          # bash — quote it, paths may contain spaces
grep -c ERROR "$LOG"
```

```python
import argparse                    # python — the error_counter.py style
p = argparse.ArgumentParser()
p.add_argument("log")
args = p.parse_args()              # or simply: sys.argv[1]
```

`node` / `ruby` / `perl` equivalents: `process.argv[2]` / `ARGV[0]` /
`$ARGV[0]`. Every job also exports `JOB_LOG_PATH` (the same input path),
`JOB_ORIGINAL_LOG` (the log as selected in the UI), `JOB_OUTPUT_DIR` and
`JOB_ID` — so a script whose command omits `{log}` can still read
`os.environ["JOB_LOG_PATH"]`, as long as a log was selected. Run without
one — a log-free script, or any submission with an empty `log_path` —
and the two log variables are absent, not empty. Scripts that simply
read their input
argument work unchanged in chains, where step 2+ receives the previous
step's output as `{log}`. Compressed logs (.gz/.bz2/.xz) are decompressed
by the server first — `{log}` is always plain text.

### Chains (pipelines)

`POST /api/jobs` with `"mode": "chain"` links the submitted steps: step 1 runs
on the selected log, each later step starts automatically when the previous one
succeeds and receives its output as `{log}`. Input resolution rule per step:

1. the previous step's declared artifact (`output_filename` / `{output}`), if it exists
2. otherwise the single file the script wrote into its `outputs/` directory
3. otherwise the previous step's `stdout.txt`

Failures stop the chain (later steps become `skipped`); cancelling any step
cancels the whole chain; re-running any step re-runs the entire chain (and
replaces the old one if it is still running) — or, with `?from_step=N` /
the **Re-run from here** button, only step N and the ones after it, seeding
step N's input from the preceding completed step's persisted output.
Deleting a step of a
still-live chain cancels the chain first — later steps may depend on that
step's output; deleting steps of a finished chain just removes them.
Submissions can carry an optional **label** (searchable in the Jobs panel).
Compressed inputs (`.gz`/`.bz2`/`.xz`) are
decompressed automatically, whether the selected log or a previous step's
output. Chained steps also
receive `JOB_ORIGINAL_LOG` (the originally selected file) in addition to
the standard environment variables. In the UI, switch the
run bar to **🔗 Chain** and order the step cards with ↑/↓ — the same script
may appear multiple times (e.g. filter → filter).

Type rendering: `boolean` → flag only when true; `multi` → repeated flag per
value; everything else → `--flag value`. Commands are tokenized with shell
rules but executed **without a shell** (exec), so parameter values are never
interpreted as shell syntax.

### Saved pipelines

A saved pipeline is a whole chain configuration — ordered scripts plus
their parameters — stored on the server in `data/pipelines.json`, so every
browser and both UIs share the same list. The lifecycle:

1. **Save** — 💾 in the runbar (either UI). Saving validates like a
   submission, so a stored pipeline always loads cleanly; the name is the
   identity (saving it again replaces it). The python UI redirects to the
   same fully-configured `/run` URL, which doubles as a shareable link —
   parameter values ride along as `p::` query params.
2. **Load** — 📂 (React) or the "Saved pipelines" card on the run page
   (python UI). Loading replaces the current steps in order, forces chain
   mode and prefills every parameter; steps whose scripts were deleted in
   the meantime are skipped. Explicit URL values win over the stored ones,
   so a shared link can tweak one knob of a loaded pipeline.
3. **Run** — nothing special: the loaded steps are submitted as a plain
   chain on any log.
4. **Delete** — 🗑 (either UI) or `DELETE /api/pipelines/{name}`.

Chains saved by older versions in the browser's localStorage move to the
server automatically on first load.

### Schedules (cron)

A schedule runs a **saved pipeline — or a single script — on a log at
cron-defined times** ("run the errors pipeline on app.log every night at
08:00", or "snapshot disk usage hourly with no log at all"). Schedules are
stored in `data/schedules.json` and fired by an in-process ticker that
wakes every 30 seconds (server-local time, minute granularity).

- **Creating one** — the Schedules panel (React, below the runbar) or the
  `/schedules` page (python UI): name, cron expression, a pipeline *or* a
  script, a log (optional — a `— none —` choice, required only when the
  first step reads `{log}`; typed as a path too — any file on the
  machine, like the run page's box, overriding the list) and an optional
  job label. Preset chips build
  common expressions (`*/5 * * * *`, hourly, daily 08:00, weekdays 09:00).
  Saving validates everything up front: the expression must parse (and
  match something within four years — Feb 30 is rejected), the pipeline
  or script must exist and still be runnable, and a given log must resolve.
  The python form's "Configure parameters" button re-renders the page with
  the chosen script's parameter fieldset (zero JS — the typed values ride
  along as query params); React shows the parameter card inline.
- **What fires** — the pipeline's steps (resolved fresh at fire time, so
  editing it updates every schedule pointing at it) or the schedule's own
  script(s), as a chain through the normal submission path: scheduled runs
  are ordinary jobs carrying the schedule's name (or its label), so job
  history, search, retention and `MAX_CONCURRENT_JOBS` all apply
  unchanged. Failures at fire time (log deleted, script removed) land in
  the schedule's last-result field.
- **Bounds, by design** — if the previous run is still active at fire
  time, the fire is skipped and recorded (`skipped: previous run still
  active`) — never two of the same schedule at once. If the server was
  down at fire time, that fire is missed: no catch-up, no retries, no
  backfill. This is a workbench, not Airflow.
- **Managing** — Run now (▶, ignores the clock), Pause/Resume, 🗑 Delete;
  the list shows each schedule's next fire, last run and last result.
  `LOGSMITH_SCHEDULER=0` disables the ticker — schedules stay listed and
  editable, and you can fire them from a system crontab hitting
  `POST /api/schedules/{name}/run` if you'd rather own the clock.

### Script runtime environment

Each script process receives:

- `argv` as rendered from the command template
- `cwd` = the job's `outputs/` directory
- `JOB_ID`, `JOB_LOG_PATH`, `JOB_OUTPUT_DIR`, `JOB_ORIGINAL_LOG` environment
  variables (`JOB_ORIGINAL_LOG` = the originally selected file, useful inside
  chains). The two log variables are absent — not empty — on log-free runs
  (no selected log, command without `{log}`)

Two ways to produce artifacts: write to the `{output}` path, or write any
files into `$JOB_OUTPUT_DIR` — both are exposed via
`GET /api/jobs/{id}/files/{name}`.

## API overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/logs` | list log files (recursive, newest first) |
| GET | `/api/logs/preview?path=…&lines=200[&tail=1]` | first (or last, with `tail=1`) N lines |
| POST | `/api/logs/upload` | multipart upload of a log file into `logs/` (409 on collision, 413 over `MAX_LOG_UPLOAD_MB`) |
| GET | `/api/scripts` | scripts + manifests |
| GET | `/api/scripts/{id}` | code + manifest of a script (for the editor) |
| POST | `/api/scripts` | create a script `{id, extension, code, manifest}` (UI editor) |
| PUT | `/api/scripts/{id}` | update code/manifest (extension change moves the file) |
| DELETE | `/api/scripts/{id}` | remove script + manifest (job history is kept) |
| GET | `/api/scripts/{id}/export` | download the script as a tar.gz backup |
| POST | `/api/scripts/import` | multipart tar.gz — the export format back (or a pack of flat scripts). `?name=` ids a directory-style archive (else the file name), `?overwrite=1` replaces existing ids. All-or-nothing: 422 bad archive, 409 existing ids, 403 when editing is locked |
| POST | `/api/jobs` | `{log_path, mode: parallel\|chain, label?, jobs: [{script_id, params}]}` → N jobs. `log_path` optional — a script reading `{log}` without one is a 422 naming it (chain mode checks step 1 only) |
| POST | `/api/jobs/prune?days=N` | delete jobs older than N days |
| GET | `/api/pipelines` | saved pipelines (reusable chains) — shared by every browser and both UIs |
| POST | `/api/pipelines` | save a pipeline `{name, steps: [{script_id, params}]}` (upsert by name, validated like a submission) |
| DELETE | `/api/pipelines/{name}` | remove a saved pipeline |
| GET | `/api/schedules` | saved schedules, each enriched with `next_due` (local time) and `cron_desc` |
| POST | `/api/schedules` | save a schedule `{name, cron, pipeline \| steps, log, label?, enabled}` (upsert by name; cron, target and log validated — a first step reading `{log}` requires a log) |
| POST | `/api/schedules/{name}/toggle` | pause/resume a schedule |
| POST | `/api/schedules/{name}/run` | fire now — the same path the ticker uses, so overlap with a live run is skipped and recorded |
| DELETE | `/api/schedules/{name}` | remove a schedule |
| GET | `/api/auth/status` | whether a password is required |
| POST | `/api/auth/login` `/logout` | password → session cookie (login is rate-limited: 5 failures/min per client) |
| POST | `/api/terminal/sessions` | (flag-gated: 404 while `LOGSMITH_TERMINAL` is off) start a shell session → `{id, shell}` (429 at `LOGSMITH_TERMINAL_MAX_SESSIONS`) |
| GET | `/api/terminal/sessions/{id}/output` | SSE stream of the session's output — `{"type":"output","data":<base64>}` frames, then one `{"type":"exit","code":N}` |
| POST | `/api/terminal/sessions/{id}/input` | raw body = keystrokes, written to the pty verbatim |
| POST | `/api/terminal/sessions/{id}/size` | `{cols, rows}` → pty resize + SIGWINCH |
| DELETE | `/api/terminal/sessions/{id}` | end the session (its whole process group) |
| GET | `/api/jobs` | list jobs (optional `?status=`, `?q=` free-text search over label/script/log/ids, `?limit=&offset=` paging; returns `total`) |
| GET | `/api/jobs/{id}` | detail incl. stdout/stderr tails |
| GET | `/api/jobs/{id}/events` | SSE stream (status + stdout deltas) |
| GET | `/api/jobs/{id}/stdout` `/stderr` | full text |
| GET | `/api/jobs/{id}/files/{name}` | download artifact |
| POST | `/api/jobs/{id}/cancel` | kill a queued/running job (a whole chain for chain jobs) |
| POST | `/api/jobs/{id}/rerun` | new job with same config (chain: re-runs the whole chain; `?from_step=N` re-runs only step N onwards, reusing persisted earlier outputs) |
| DELETE | `/api/jobs/{id}` | delete job record (cancels the chain first if still live) |

Interactive docs: https://localhost:8000/docs · full request/response shapes:
[SPECS.md](SPECS.md#5-api-reference)

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LOGS_DIR` | `./logs` | where log files are listed from |
| `SCRIPTS_DIR` | `./scripts` | where scripts/manifests are discovered |
| `DATA_DIR` | `./data` | job records, stdout/stderr, artifacts |
| `MAX_CONCURRENT_JOBS` | `4` | process pool size |
| `DEFAULT_TIMEOUT` | `600` | fallback timeout (manifest wins) |
| `LOG_PREVIEW_LINES` | `200` | default lines in the log preview modal |
| `STDOUT_TAIL_BYTES` | `32768` | stdout/stderr bytes inlined in job detail |
| `MAX_LOG_UPLOAD_MB` | `512` | max size of a browser log upload (0 disables uploads) |
| `LOGSMITH_TOKEN` | *(unset)* | password for the UI; when unset and the bind is non-local, the launcher generates one into `.run/authtoken` |
| `LOGSMITH_INSECURE` | `0` | launcher escape hatch: `1` keeps a non-local server open with no password (not recommended) |
| `LOGSMITH_ALLOW_SCRIPT_EDITING` | `1` | `0` locks the script editor: write endpoints answer 403 in both UIs, existing scripts stay runnable |
| `LOGSMITH_DOCKER_SANDBOX` | `0` | `1` runs every script in a throwaway Docker container — no network, memory-capped, mounts limited to script + input + outputs |
| `LOGSMITH_SANDBOX_IMAGE` | `logsmith-sandbox:latest` | image for sandboxed runs (built from `sandbox.Dockerfile` by run.sh; extend it for extra libraries) |
| `LOGSMITH_SANDBOX_MEMORY` | `512m` | per-job memory cap inside the sandbox |
| `LOGSMITH_HOST` | `0.0.0.0` | address the server binds — all interfaces by default; `127.0.0.1` restores local-only |
| `LOGSMITH_UI` | `react` | which frontend the backend serves: `react` (built SPA) or `python` (server-rendered pages) — same as `run.sh --ui` |
| `LOGSMITH_SCHEDULER` | `1` | `0` disables the cron ticker — schedules stay listed and editable, they just never fire on their own |
| `LOGSMITH_TERMINAL` | `0` | `1` enables the interactive shell on the server in the browser: React's Terminal tab, and the python UI's `/terminal` page (same shell as progressive enhancement — its no-JS command box stays as the fallback). Also off whenever `LOGSMITH_ALLOW_SCRIPT_EDITING=0` |
| `LOGSMITH_TERMINAL_MAX_SESSIONS` | `4` | concurrent shell sessions (429 when full; strays are reaped by the idle timeout) |
| `LOGSMITH_TERMINAL_IDLE_TIMEOUT` | `1800` | seconds without input or output before a session is killed (0 disables the reaper) |
| `LOGSMITH_CERT_HOSTS` | *(unset)* | extra TLS SANs (comma-separated IPs/names) for reaching the app from other devices — delete `certs/` to apply |
| `JOB_RETENTION_DAYS` | `0` | auto-delete jobs older than N days at startup (0 = keep forever) |

Example: `LOGSMITH_TOKEN=s3cret JOB_RETENTION_DAYS=14 ./run.sh`

`LOGSMITH_*` variables may also live in the repo's `.env` — run.sh reads only
the lines it recognizes (`LOGSMITH_…=…`; anything else in the file, other
tools' keys included, stays untouched) and the real environment wins over the
file. Example `.env` line: `LOGSMITH_TERMINAL=1`.

## Tests

```bash
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests -q   # backend
cd frontend && npm test                       # vitest (pure UI logic)
```

The suite runs against the real sample scripts with temporary `logs/` and
`data/` directories — it never touches your actual job history. Coverage:
job store persistence/reconcile/prune (plus the parse cache), executor
lifecycle (timeouts, cancel, gz/bz2/xz decompression), chain advancement/cancel
invariants, the full HTTP API (atomic validation, labels + search, status
filtering, paging, SSE streaming, the step-input endpoint, full and partial
re-run), submission pre-flight + spawn-error diagnostics (missing
interpreters named), the async contract (parallel jobs overlap; the API
answers mid-run), log preview (head/tail/compressed), upload sanitization
and caps, saved pipelines (API round-trip + validation, the python UI's
save/load flow), authentication incl. rate limiting and session expiry, the
python UI's page flow (login, submission, fan-out, chains, compare,
editor, the output table + CSV), plus the frontend's diff/table logic. See
[ARCHITECTURE.md §11](ARCHITECTURE.md#11-testing) for the invariant → test
mapping. CI installs the pinned `backend/requirements.lock`, runs both
suites and the frontend build on every push (`.github/workflows/tests.yml`).

## Security notes

This is a trusted-operator tool: scripts run **as the server user** with full
permissions of whatever account runs uvicorn. Parameter values are passed as
literal argv tokens (no shell injection), and log paths are validated to stay
inside `LOGS_DIR`, but the scripts themselves are arbitrary code.

Mitigations built in:

- **Authentication** via `LOGSMITH_TOKEN` (recommended before any LAN exposure)
- Bind address configurable via `LOGSMITH_HOST` (default `0.0.0.0` — all
  interfaces; the start script warns loudly when exposed with no token);
  HTTPS with a local certificate
- **The terminal is opt-in** (`LOGSMITH_TERMINAL`, default off = hidden:
  404s, no nav links). It is a shell as the server user — anyone with the
  password can run anything, which is why it also refuses to turn on while
  `LOGSMITH_ALLOW_SCRIPT_EDITING=0` is set. Sessions die with their browser
  tab, on explicit close, or after `LOGSMITH_TERMINAL_IDLE_TIMEOUT` (30 min
  default) with no input and no output.
- Markdown artifacts are sanitized with DOMPurify before rendering

Still do not expose it directly to the internet — put it behind a VPN or a
reverse proxy with proper access control if you need remote access.

## Regenerating sample logs

```bash
python3 tools/generate_sample_logs.py
```
