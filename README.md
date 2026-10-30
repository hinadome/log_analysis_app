# LogSmith — Log Analysis Workbench

A web application for running server-side analysis scripts against server-side
log files. Pick a log, pick one or more scripts, set parameters, run — jobs
execute asynchronously with live output streaming, and results (including
artifact files) are viewable in the browser.

**Docs:** [SPECS.md](SPECS.md) — normative functional/API spec ·
[ARCHITECTURE.md](ARCHITECTURE.md) — system design and chain-engine internals

## Quick start

```bash
# 1. Backend (Python 3.10+)
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# 2. Sample data (skippable if you point LOGS_DIR/SCRIPTS_DIR at your own dirs)
python3 tools/generate_sample_logs.py

# 3a. Production-style: build the frontend once, backend serves it
cd frontend && npm install && npm run build && cd ..
./run.sh                                          # https://localhost:8000

# 3b. Or dev mode with hot reload (frontend on :5173, proxies /api)
.venv/bin/uvicorn --app-dir backend app.main:app --reload &
cd frontend && npm run dev                          # http://localhost:5173
```

## HTTPS

`run.sh` serves the app over **https://localhost:8000**. On first start it
generates a local certificate into `certs/` (via `tools/generate_cert.sh`) and
reuses it on every later start — it is never regenerated while it exists.

- **With [mkcert](https://github.com/FiloSottile/mkcert) installed**
  (`brew install mkcert`): the certificate is trusted by your browser, no
  warnings. Delete `certs/` and restart to upgrade an existing self-signed
  cert to a trusted one.
- **Without mkcert**: a self-signed certificate (SANs: `localhost`,
  `127.0.0.1`, `::1`, valid 825 days) is generated with openssl. Browsers show
  a one-time "not private" warning — proceed or install mkcert.
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

- **Async execution** — jobs get an ID immediately and run in a bounded pool
  (`MAX_CONCURRENT_JOBS`, default 4). Output streams live into the job view.
- **Live streaming** — the job detail page uses server-sent events
  (`GET /api/jobs/{id}/events`) for zero-latency stdout while a script runs,
  with automatic fallback to polling.
- **File artifacts** — anything a script writes to its `outputs/` directory is
  viewable/downloadable in the browser; Markdown files render nicely.
- **Authentication** — set `LOGSMITH_TOKEN` to require a password for the UI
  (browser login screen, HttpOnly session cookie). Automation can send
  `Authorization: Bearer <token>`. Unset = no auth (localhost only!).
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
- **Saved pipelines** — save a whole ordered chain (scripts + parameters)
  under a name and load it on any log, like parameter presets but for chains
  (stored in your browser's localStorage).
- **Labels & job search** — tag a submission with a label ("timeout spike
  investigation") at creation time and filter the job history by label,
  script, log or free text.
- **Script editor** — create, edit and delete scripts in the browser: code
  textarea, manifest form (name, command, timeout, output file) and a JSON
  parameter editor. Writes ordinary flat-style files into `scripts/`, so
  hand-added scripts and UI-created ones are interchangeable. The bundled
  samples cover every editor language — Python, Bash, Node, Ruby and Perl —
  and double as reference templates.
- **Notifications** — enable the 🔔 button in the top bar to get a browser
  notification when a job finishes.
- **Parameter presets** — save/load named parameter combos per script
  (stored in your browser's localStorage).
- **Test suite** — `backend/tests/` (pytest) locks in the chain invariants,
  the API contract and the storage layer. See
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
              React UI (Vite)  ◄── 4. poll GET /api/jobs/{id} (live tail + SSE)
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
ones on disk.

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
| GET | `/api/logs/preview?path=…&lines=200` | first N lines |
| GET | `/api/scripts` | scripts + manifests |
| GET | `/api/scripts/{id}` | code + manifest of a script (for the editor) |
| POST | `/api/scripts` | create a script `{id, extension, code, manifest}` (UI editor) |
| PUT | `/api/scripts/{id}` | update code/manifest (extension change moves the file) |
| DELETE | `/api/scripts/{id}` | remove script + manifest (job history is kept) |
| POST | `/api/jobs` | `{log_path, mode: parallel\|chain, label?, jobs: [{script_id, params}]}` → N jobs |
| POST | `/api/jobs/prune?days=N` | delete jobs older than N days |
| GET | `/api/auth/status` | whether a password is required |
| POST | `/api/auth/login` `/logout` | password → session cookie |
| GET | `/api/jobs` | list jobs (optional `?status=`, `?q=` free-text search over label/script/log/ids) |
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
| `LOGSMITH_TOKEN` | *(unset)* | password for the UI; unset disables auth (localhost only) |
| `JOB_RETENTION_DAYS` | `0` | auto-delete jobs older than N days at startup (0 = keep forever) |

Example: `LOGSMITH_TOKEN=s3cret JOB_RETENTION_DAYS=14 ./run.sh`

## Tests

```bash
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests -q
```

The suite runs against the real sample scripts with temporary `logs/` and
`data/` directories — it never touches your actual job history. Coverage:
job store persistence/reconcile/prune, executor lifecycle (timeouts, cancel,
decompression), chain advancement/cancel invariants, and the full HTTP API
(atomic validation, labels + search, full and partial re-run). See
[ARCHITECTURE.md §11](ARCHITECTURE.md#11-testing) for the invariant → test
mapping.

## Security notes

This is a trusted-operator tool: scripts run **as the server user** with full
permissions of whatever account runs uvicorn. Parameter values are passed as
literal argv tokens (no shell injection), and log paths are validated to stay
inside `LOGS_DIR`, but the scripts themselves are arbitrary code.

Mitigations built in:

- **Authentication** via `LOGSMITH_TOKEN` (recommended before any LAN exposure)
- Server binds to `127.0.0.1` only; HTTPS with a local certificate
- Markdown artifacts are sanitized with DOMPurify before rendering

Still do not expose it directly to the internet — put it behind a VPN or a
reverse proxy with proper access control if you need remote access.

## Regenerating sample logs

```bash
python3 tools/generate_sample_logs.py
```
