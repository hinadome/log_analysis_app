# LogSmith — Log Analysis Workbench

A web application for running server-side analysis scripts against server-side
log files. Pick a log, pick one or more scripts, set parameters, run — jobs
execute asynchronously with live output streaming, and results (including
artifact files) are viewable in the browser.

**Docs:** [SPECS.md](SPECS.md) — normative functional/API spec ·
[ARCHITECTURE.md](ARCHITECTURE.md) — system design and chain-engine internals

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
differences (short version: presets, saved pipelines and notifications
are React-only; the python UI trades them for deep-linkable URLs and no
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
  warning. To include them, delete `certs/` and regenerate with their address:
  `LOGSMITH_CERT_HOSTS='192.168.1.50,my-mac.local' ./run.sh start`.
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
  (`MAX_CONCURRENT_JOBS`, default 4). Output streams live into the job view.
- **Deep links** — every job has a URL (`#/job/<id>`); refresh, the browser
  back button and shared links all land on the right view.
- **Live streaming** — the job detail page uses server-sent events
  (`GET /api/jobs/{id}/events`) for zero-latency stdout while a script runs,
  with automatic fallback to polling.
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
  before scripts run, and preview correctly in the UI.
- **Job retention** — `JOB_RETENTION_DAYS` auto-deletes old jobs at startup
  (0 = off); the Jobs panel has a manual 🧹 Prune button and shows storage use.
- **Chain mode (pipelines)** — submit the same scripts as an ordered chain:
  steps run sequentially and each step's `{log}` is the previous step's output
  (its single artifact file, or its stdout when no artifact was produced).
  A failed step skips the rest; cancelling any step cancels the chain; re-running
  any step re-runs the whole chain. `JOB_ORIGINAL_LOG` always points at the
  original file. Scripts need **zero changes** to work in chains.
- **Continue-from-step-N** — re-run only from a chosen step onwards: earlier
  steps' persisted outputs are reused, so iterating on step 3 doesn't pay for
  step 1 again ("Re-run from here" on any chain step).
- **Step-input viewer** — the Input tab shows exactly what a script received:
  the selected log, or for chain steps the previous step's actual output
  (compressed sources shown decompressed).
- **Saved pipelines** *(React UI)* — save a whole ordered chain (scripts +
  parameters) under a name and load it on any log, like parameter presets
  but for chains (stored in your browser's localStorage).
- **Fan-out over logs** — arm extra logs with the ＋ toggle in the log list
  and one Run submits the same scripts against each of them ("3 logs × 2
  steps = 6 jobs"), e.g. the same counter across all rotated files.
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
  non-scary. The bundled samples cover every editor language — Python, Bash
  (`.sh`/`.bash`/`.zsh`), Node, Ruby and Perl — and double as reference
  templates.
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

Scripts are discovered in two shapes:

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

### Command placeholders

| Placeholder | Expands to |
|---|---|
| `{script}` | absolute path of the script |
| `{log}` | absolute path of the selected log — in chain mode, the **previous step's output** for steps 2+ |
| `{output}` | `<job>/outputs/<output_filename>` — file artifacts |
| `{args}` | rendered CLI flags from validated parameters |

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
Submissions can carry an optional **label** (searchable in the Jobs panel),
and the whole ordered step list can be saved as a named **pipeline preset**
(💾 Save / 📂 Load in chain mode). Compressed inputs (`.gz`/`.bz2`/`.xz`) are
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

### Script runtime environment

Each script process receives:

- `argv` as rendered from the command template
- `cwd` = the job's `outputs/` directory
- `JOB_ID`, `JOB_LOG_PATH`, `JOB_OUTPUT_DIR`, `JOB_ORIGINAL_LOG` environment
  variables (`JOB_ORIGINAL_LOG` = the originally selected file, useful inside
  chains)

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
| POST | `/api/jobs` | `{log_path, mode: parallel\|chain, label?, jobs: [{script_id, params}]}` → N jobs |
| POST | `/api/jobs/prune?days=N` | delete jobs older than N days |
| GET | `/api/auth/status` | whether a password is required |
| POST | `/api/auth/login` `/logout` | password → session cookie (login is rate-limited: 5 failures/min per client) |
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
| `LOGSMITH_CERT_HOSTS` | *(unset)* | extra TLS SANs (comma-separated IPs/names) for reaching the app from other devices — delete `certs/` to apply |
| `JOB_RETENTION_DAYS` | `0` | auto-delete jobs older than N days at startup (0 = keep forever) |

Example: `LOGSMITH_TOKEN=s3cret JOB_RETENTION_DAYS=14 ./run.sh`

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
re-run), log preview (head/tail/compressed), upload sanitization and caps,
authentication incl. rate limiting and session expiry, the python UI's page
flow (login, submission, fan-out, chains, compare, editor, the output table
+ CSV), plus the frontend's diff/table logic. See
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
- Markdown artifacts are sanitized with DOMPurify before rendering

Still do not expose it directly to the internet — put it behind a VPN or a
reverse proxy with proper access control if you need remote access.

## Regenerating sample logs

```bash
python3 tools/generate_sample_logs.py
```
