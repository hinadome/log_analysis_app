# LogSmith Architecture

How LogSmith is built and why. For the normative behavior specification (API
shapes, chain rules, manifest fields) see [SPECS.md](SPECS.md); for usage and
setup see [README.md](README.md).

## 1. System overview

```
                          ┌───────────────────────────────────────────────┐
                          │            Browser (SPA or pages)             │
                          │  React UI (default) or the python UI — both   │
                          │  poll jobs; the SPA adds SSE live stdout      │
                          └───────────────┬───────────────────────────────┘
                                          │ HTTPS (cookie or Bearer token)
                                          ▼
┌─────────────┐   list/preview   ┌─────────────────────────────────────────┐
│   logs/     │◄────────────────►│              FastAPI backend            │
│  LOGS_DIR   │                  │                                         │
└─────────────┘                  │  auth middleware ─► routers             │
┌─────────────┐   discover       │    /api/auth    (login/logout/status)   │
│  scripts/   │◄────────────────►│    /api/logs    (list/preview)          │
│ + manifests │                  │    /api/scripts (discover)              │
└─────────────┘                  │    /api/jobs    (create/list/detail/    │
        │                        │                   SSE/cancel/rerun/    │
        │ {script} argv          │                   delete/prune)         │
        ▼                        │                                         │
┌────────────────┐  job records  │            services                     │
│  Executor      │◄─────────────►│  scanner    discovery of logs+scripts   │
│  (async engine)│               │  job_store  filesystem persistence      │
│                │  subprocesses  │  executor   execution + chain engine    │
│  ┌──────────┐  │               └───────────────┬─────────────────────────┘
│  │ python3  │  │  stdout/stderr streams         │ job.json per job
│  │ bash     │  │────────────────────────────────▼
│  │ node …   │  │                        ┌──────────────────┐
│  └──────────┘  │                        │   data/jobs/     │
└────────────────┘                        │  <id>/job.json   │
        ▲ artifacts into outputs/         │  <id>/stdout.txt │
        └──────────────────────────────────│  <id>/outputs/… │
                                           └──────────────────┘
```

Everything is a single uvicorn process on one asyncio event loop. Scripts run
as real subprocesses (`create_subprocess_exec`, **no shell**), bounded by a
semaphore; the UI is a static React bundle served by the same process.

## 2. Module map

```
backend/app/
├── main.py            App assembly: auth middleware, routers, lifespan
│                      (reconcile → prune on start; executor.shutdown on stop),
│                      and the frontend mount: the React SPA from frontend/dist
│                      or the python pages router, per LOGSMITH_UI
├── config.py          All settings, read from environment variables once
├── models.py          Pydantic models: ParamSpec, ScriptManifest, ScriptInfo,
│                      LogFile, JobSpecIn, JobCreateRequest, Job (incl. chain
│                      fields), JobStatus literal
├── auth.py            Shared-password auth: HttpOnly cookie session +
│                      Bearer token; constant-time comparisons; per-IP
│                      login rate limiting (5 failures/60s → 429);
│                      check_password/apply_session_cookie shared by the
│                      JSON API login and the HTML form login
├── pages.py           The python UI (LOGSMITH_UI=python): Jinja2 pages that
│                      call the routers' functions directly; templates in
│                      app/templates/, styles in app/static/
├── routers/
│   ├── logs.py        GET /api/logs, GET /api/logs/preview (head + tail,
│   │                  transparent decompression for .gz/.bz2/.xz),
│   │                  POST /api/logs/upload (multipart, streamed, capped)
│   ├── scripts.py     GET /api/scripts; editor endpoints (create/update/
│   │                  delete flat-style scripts, constrained ids so writes
│   │                  can never escape SCRIPTS_DIR); GET …/export (tar.gz
│   │                  backup)
│   └── jobs.py        Job lifecycle endpoints; two-pass validation for
│                      create/rerun; chain-aware cancel/delete/rerun;
│                      ?q=/?status= filters + limit/offset paging
└── services/
    ├── scanner.py     Discovers logs (recursive, newest first) and scripts
    │                  (flat + directory styles, default commands)
    ├── job_store.py   Filesystem persistence; parse cache keyed by
    │                  (mtime, size); list_chain; reconcile; prune
    └── executor.py    Execution engine + chain (pipeline) engine

backend/tests/         pytest suite: conftest isolates LOGS_DIR/DATA_DIR to
                       temp dirs before any app import; see §11

frontend/src/
├── App.jsx            State orchestration: auth, selection, ordered steps,
│                      parallel|chain mode, submission label, chain presets,
│                      job polling, notifications
├── api.js             Fetch wrapper; emits a global event on 401
├── presets.js         Named parameter presets + saved chains (localStorage)
├── hooks.js           useInterval
├── fmt.js             Byte formatting
└── components/
    ├── LoginView.jsx      Password screen
    ├── LogPicker.jsx      Log list + upload dropzone + preview modal
    │                      (Head ⇡ / Tail ⇣ toggle)
    ├── ScriptPicker.jsx   Script cards (click = add a step), ＋ New /
    │                      ✏️ edit opening the script editor
    ├── ScriptEditor.jsx   Browser editor: code, manifest fields, JSON
    │                      parameters — writes flat-style files via the API;
    │                      copy-code + tar.gz backup buttons
    ├── ParamForm.jsx       Per-step parameter card, ↑/↓ ordering, presets
    ├── JobsPanel.jsx       Job list: badges, server-side search (debounced
    │                      in App), Load-more paging, chain "step 2/3"
    │                      chip, labels, prune
    ├── JobDetail.jsx      SSE live view, chain strip, markdown artifacts,
    │                      copy-as-command, re-run (full / from here)
    │                      ⇄ Compare (opens DiffView)
    └── DiffView.jsx       Unified line diff (hand-rolled LCS, 2000-line
                           cap) of two completed jobs' stdout
```

## 3. Job execution engine (`services/executor.py`)

### 3.1 Single job lifecycle

1. **`submit(job, script)`** renders the final argv from the manifest command
   template (`{script}`, `{log}`, `{output}`, `{args}`) and persists it, then
   schedules `_run()` as an asyncio task. The rendered `command` is stored on
   the job record — the UI can show exactly what ran.
2. **`_run()`** acquires the concurrency semaphore (`MAX_CONCURRENT_JOBS`),
   materializes the input (see §3.3), spawns the subprocess with
   `stdout=PIPE, stderr=PIPE, cwd=<job>/outputs/`, and pumps both streams to
   `stdout.txt` / `stderr.txt` in 64 KB chunks so the UI can tail them live.
3. **Timeout** is enforced with `asyncio.wait_for` around the pumps; expiry
   kills the process and the job fails with `Timed out after N seconds`.
4. **Completion** sets the terminal status (`completed` for exit 0, else
   `failed` with the exit code), records sizes and artifact names, and — for
   chain steps — runs chain bookkeeping (§4).
5. **Cancellation** (`cancel()`) marks the job in a `_cancelling` set, kills
   the process if it has one, otherwise cancels the task. The status becomes
   `cancelled` rather than `failed` so users can distinguish intent.

### 3.2 Input resolution (unified for logs and chained inputs)

`input_path()` (public — the `/input` endpoint uses the same resolver) /
`_materialize_input()` treat the selected log and a chained
previous-step output identically:

```
source = job.input_path or job.log_path
if source is .gz/.bz2/.xz → decompress once into <job>/input.log
else                        → use the file directly
```

A compressed input is therefore **never** handed to a script raw — whether it
is the originally selected log or step N−1's gzipped artifact.

### 3.3 Script runtime contract

Each script process receives `argv` from the rendered template, `cwd` = the
job's `outputs/` directory, and env vars `JOB_ID`, `JOB_LOG_PATH` (the
resolved, decompressed input), `JOB_OUTPUT_DIR`, and `JOB_ORIGINAL_LOG` (the
originally selected file — chain steps can still reach it). Artifacts are
whatever the script writes to `{output}` or into `$JOB_OUTPUT_DIR`.

## 4. Chain (pipeline) engine

Chains add sequencing and data hand-off on top of the plain job machinery —
scripts themselves are unchanged.

### 4.1 Submission — atomic, two-pass

`POST /api/jobs` with `mode: "chain"` validates **every** step (script exists,
manifest healthy, parameters type-check) before creating any job records.
Only then are the jobs created, linked with `chain_id` / `chain_index` /
`chain_size`, and handed to
`submit_chain(steps, chain_id, start_index=0, total=None)`, which:

- runs step 0 immediately (its input is the selected log, or an `input_path`
  seeded by a partial re-run), and
- parks steps 1+ as `waiting` — a purely on-disk state with **no task**.

`start_index` / `total` exist for continue-from-step-N re-runs: a partial
re-run of steps 3..5 of a 5-step chain passes `start_index=2, total=5` so
the new chain still reads "step 3/5" in the UI.

A rejected submission therefore creates zero jobs; there are no orphaned
"queued" records.

### 4.2 Lazy command rendering

Step N's command cannot be rendered at submission time — its input file only
exists once step N−1 finishes. Rendering happens in `submit()` at the moment
the step actually starts, with the resolved `input_path` baked into `{log}`.

### 4.3 Advancement

When a step completes, `_advance_chain()`:

1. checks the chain-cancel flag (§4.5) — if set, skips the rest instead of
   handing off;
2. finds the **next surviving `waiting` step with a higher index**
   (`_next_waiting_step`) — gap-tolerant, so a deleted middle step cannot
   stall later steps forever;
3. resolves what the completed step passes on (`chain_output` — public,
   because the rerun endpoint also uses it to seed partial re-runs):
   the declared `{output}` artifact if it exists → else the single file in
   `outputs/` → else `stdout.txt` (non-empty);
4. fails the chain if there is nothing to pass on (`Previous step produced no
   output`) or the next step's script has disappeared, marking the step
   `failed` and skipping the rest;
5. otherwise records `input_path` / `input_source` on the next step and
   submits it.

### 4.4 Failure handling

A step that ends `failed` or `cancelled` triggers `_skip_remaining()`: every
later `waiting` step becomes `skipped` with a reason. Skipping is idempotent
(it only touches `waiting` steps), so the runner's finally-block skip and a
concurrent `cancel_chain` skip can safely overlap.

### 4.5 Cancellation — race-safe by construction

`cancel_chain()` sets `_cancelling_chains[chain_id]` **before** killing
anything. This closes the hand-off race: a step completing while the cancel
is in flight cannot advance the chain, because `_advance_chain()` re-checks
the flag and skips the rest instead. The flag is never cleared — chain ids
are unique per submission, so a stale flag can only guard a dead chain.

The kill loop targets steps that are `queued`/`running` **or** still own a
live task (the brief window where a step has been submitted but not yet
flipped to `running`); pure `waiting` steps simply flip to `skipped` on disk.

### 4.6 Endpoint semantics for chains

- **cancel any step** → `cancel_chain` (kill active, skip waiting)
- **rerun any step** → validates all steps, cancels the old chain if still
  live, submits a fresh chain with the same specs
- **rerun from step N** (`?from_step=`) → same validation, but only steps
  N+ are re-created. The first one is seeded with the nearest preceding
  surviving step's `chain_output()` — a persisted artifact/stdout, so earlier
  steps are not re-executed. `submit_chain(start_index, total)` keeps the
  original numbering ("step 3/5" stays "step 3/5"); a non-completed
  predecessor is rejected with 409 before anything is created or cancelled
- **delete a step** → if the chain is still alive (any step
  queued/waiting/running), cancels the chain first — later steps may consume
  this step's output; deleting steps of a finished chain just removes them

### 4.7 Shutdown, restart and re-entry

`shutdown()` sets `_shutting_down` before cancelling tasks. The runner's
finally block does **no** chain bookkeeping when it is set, so no task or
subprocess is spawned after the gather has completed. Leftover `waiting`
steps stay on disk and are marked `failed: "Interrupted by server restart"`
by `JobStore.reconcile()` on the next start; a step that was actively killed
by shutdown records `cancelled: "Cancelled by server shutdown"`.

`start()` (called from the application lifespan) resets `_shutting_down`
so a process that re-enters the app — e.g. one TestClient per test — gets a
fully armed executor instead of one stuck in shutdown state.

## 5. Job state machine

```
                 submit (task scheduled)
   created ────────────────► queued ───────────► running ──────► completed
                              │        semaphore      │  exit 0
                              │                       ├─► failed     (exit ≠ 0,
                              │                       │               timeout,
                              │                       │               spawn error)
                              │   cancel             │   cancel
                              └───────► cancelled ◄──┴──────► cancelled
                                                     
 chain steps 1+ only:               predecessor succeeded ──► queued → running → …
   created ──► waiting ──┬────────────────────────────────────►
                         ├─ predecessor failed/cancelled ──► skipped
                         └─ chain cancelled ────────────────► skipped
```

Terminal states: `completed`, `failed`, `cancelled`, `skipped`.
On restart, `reconcile()` maps any `queued`/`waiting`/`running` job to
`failed: "Interrupted by server restart"` — jobs never survive a restart.

## 6. Persistence (`services/job_store.py`)

One directory per job, JSON as the source of truth, no database:

```
data/jobs/20261029-110333-88914512/     # id = timestamp + 8 hex chars
├── job.json        # the full Job record, rewritten on every transition
├── stdout.txt      # streamed as the script runs
├── stderr.txt
├── input.log       # decompressed copy — only for compressed inputs
└── outputs/        # script cwd; artifacts served via /api/jobs/{id}/files/
```

`list()` scans directories and returns parsed jobs sorted newest-first. To
keep the 1.5 s polling loop cheap at scale, parsed `job.json` files are
cached in memory keyed by `(mtime_ns, size)`: the directory listing is still
the source of truth (deleted/wiped jobs can never leak from the cache),
every `save()` bumps the mtime so exactly one file re-parses per update, and
callers receive copies so mutating a listed job can't corrupt the cache.
`list_chain()` filters by `chain_id` and sorts by `chain_index`. Writes that
race a deletion fail silently (OSError is swallowed in `save`) — a deleted
job is never resurrected by an in-flight runner. `prune()` removes job dirs
older than the retention window using the timestamp embedded in the id.

## 7. Live streaming (SSE)

`GET /api/jobs/{id}/events` streams `text/event-stream`: it replays the last
64 KB of stdout, then polls the job every 400 ms, emitting `stdout` deltas
and `status` events until the job reaches a terminal state (a `waiting`
chain step stays open — it is not terminal). The frontend uses
`EventSource` with an automatic fallback to polling. The same files power
`GET /api/jobs/{id}/stdout|stderr` (capped at 20 MB per read).

## 8. Frontend architecture

Two interchangeable frontends serve the same `/api` and the same session
cookie; `LOGSMITH_UI` (`run.sh --ui`) picks one at import time in
`main.py`. They are mutually exclusive because the SPA owns a catch-all
route that would otherwise swallow the page routes.

### React SPA (default)

Single-page React app; state lives in `App.jsx`:

- **Routing**: the URL hash is the source of truth — `#/` is home,
  `#/job/<id>` the job view (`useHashRoute` + `parseRoute` validating the job
  id shape). Refresh, browser back/forward and shareable job links all work
  without server-side routing.
- **Selection model**: steps are an ordered array of `{key, scriptId,
  params}` — the same script may appear multiple times. In chain mode the
  order is the pipeline order (↑/↓ buttons); in parallel mode it is
  submission order only.
- **Fan-out**: LogPicker's ＋ toggles arm extra logs; Run submits the same
  steps once per armed log (frontend loop over the unchanged submit API) and
  the runbar previews "N logs × M steps = K jobs".
- **Job polling** at 1.5 s while any job is active, 5 s otherwise; job
  completions raise toasts and (with permission) browser notifications.
- **Job history** is filtered, searched and paged server-side: the search box
  drives `GET /api/jobs?q=` through a 250 ms debounce and status chips drive
  `?status=` (both kept in refs so polling retains the active query);
  "Load more" grows the fetch window (`limit`, offset stays 0 so the newest
  jobs are always in view).
- **JobDetail** consumes the SSE stream, renders the chain strip (clickable
  per-step chips with status icons), markdown artifacts (sanitized with
  DOMPurify), JSON-array output as a sortable table with CSV download, the
  Input tab (lazy `GET /input` — what the script actually received), a
  copyable reconstruction of the command line, and "⇄ Compare" (DiffView:
  LCS line diff of two completed jobs' stdout).
- **ScriptEditor** creates/edits/deletes scripts, duplicates them into new
  ids, and can test-run: ▶ Run… saves silently and submits the script with
  its manifest defaults on a chosen log, then navigates to the job.
- **LogPicker** includes a drag-and-drop upload zone (multiple files,
  streamed to `POST /api/logs/upload`) and Head/Tail preview toggles.
- **Presets** (`presets.js`) store named parameter sets per script in
  localStorage — client-side only, so this (plus saved pipelines and
  browser notifications) has no python-UI equivalent.

### Python pages (`--ui python`)

Server-rendered Jinja2 templates (`app/pages.py`, `app/templates/`,
`app/static/style.css`) — no Node, no build step, every view a plain
deep-linkable URL. Design points:

- **Direct calls, not HTTP round-trips**: handlers call the API routers'
  functions (`jobs_api.create_jobs`, `logs_api.upload_log`, …) directly.
  Same validation, same behavior, one auth story — but an `HTTPException`
  raised inside renders as the HTML error page with a back link, not JSON.
- **Auth**: `/login` is an HTML form posting to the same rate-limited
  `check_password`/`apply_session_cookie` pair the JSON API uses
  (`auth.py`); pages redirect to `/login?next=…` when a session is missing.
- **Forms over fetch**: state changes are POST forms that redirect (303)
  — the run config (`/run?logs=&scripts=` is a shareable GET), the
  submission fan-out (`/submit` posts one `JobCreateRequest` per selected
  log), cancel/delete/rerun/prune on the jobs pages, and the editor with
  two submit buttons (Save; "Save & test run" via HTML5 `formaction`).
- **Parameter fields** are named `p::<script_id>::<param>`; booleans are a
  hidden `false` plus a checkbox `true` (Starlette's `FormData.get` returns
  the last value, so the checkbox wins when checked); multi values are
  comma-separated; empty always means "manifest default".
- **OutputView parity**: finished jobs render stdout through
  `_output_view` exactly like the React component — a JSON array of flat
  objects becomes a sortable table (server-side sort via `?sortcol=
  &sortdir=`, cycling asc → desc → natural order), other JSON is
  pretty-printed, text stays plain; the Table/Raw toggle is `?view=raw`
  and `/jobs/{id}/result.csv` downloads the table sorted as shown.
- **Liveness without SSE**: active job pages (and the jobs list with
  active jobs) carry a `<meta http-equiv="refresh" content="2">`, so
  running output tails itself every 2 s; raw endpoints stay one click
  away (`/api/jobs/{id}/stdout` opens in a tab).
- **Compare** (`/jobs/{id}/compare`) diffs two completed jobs' stdout with
  `difflib.SequenceMatcher`, capped at 2000 lines per side like DiffView;
  the other job defaults to the latest run of the same script.

## 9. Security model

- **Auth**: optional shared password (`LOGSMITH_TOKEN`). Browser login sets
  an HttpOnly, SameSite=Lax cookie (Secure on HTTPS, expires after 7 days);
  automation uses `Authorization: Bearer`. Comparisons are constant-time.
  Failed logins are rate-limited per client IP (5 per 60 s sliding window →
  429 with `Retry-After`); a successful login clears the counter, and the
  Bearer path is unaffected. **Secure by default**: when unset and the bind
  is non-local, the launcher generates a strong token into `.run/authtoken`
  (gitignored, mode 600, reused across restarts) — an exposed server is
  never open unless `LOGSMITH_INSECURE=1` says so explicitly. A local bind
  skips auth entirely.
- **No shell**: commands are tokenized with `shlex` but executed via `exec`
  — parameter values are literal argv tokens and can never inject syntax.
- **Path containment**: log paths resolve strictly inside `LOGS_DIR`.
- **Uploads**: multipart log uploads are reduced to a single safe path
  component (`^[\w][\w.\- ]{0,254}$`) inside `LOGS_DIR`, streamed with a
  size cap, and staged as a hidden `.name.part` until complete — no
  half-written files in listings, no traversal, no unbounded disk use.
- **Artifact rendering** is sanitized (DOMPurify) before display.
- **Script authoring via the API** writes only flat-style files with
  id/extension-constrained names inside `SCRIPTS_DIR` — no path traversal;
  the code itself is arbitrary, which is the same trust level scripts already
  have (they run as the server user by design).
- **Script authoring lock** (`LOGSMITH_ALLOW_SCRIPT_EDITING=0`): authoring
  is remote code execution by design, so it can be switched off without
  losing anything else — every write endpoint (API and both UIs) answers
  403, reads/export/job submission keep working, and `/api/health` reports
  `script_editing: false` so the UIs hide their editor entry points.
- Trust boundary: scripts are **arbitrary code running as the server user**
  by design — this is a trusted-operator tool, not multi-tenant. Anyone with
  the password can run the scripts; with the lock on, that is all they can
  do. Run the server under a dedicated non-admin account when in doubt.

## 10. Design invariants

These are the guarantees the chain restructure introduced and the tests
verify:

1. **Cancel is chain-atomic** — once a chain cancel returns, no step of that
   chain newly starts, regardless of completion-timing races.
2. **No stalled chains** — advancement is gap-tolerant; a chain never waits
   forever on a deleted step.
3. **Atomic submission** — a rejected `POST /api/jobs` or rerun creates
   zero jobs.
4. **No raw compressed input** — `.gz/.bz2/.xz` from any source is
   decompressed before a script sees it.
5. **No orphaned work at shutdown** — no tasks or subprocesses outlive
   `shutdown()`; interrupted chains are reconciled on the next start.
6. **No dangling consumers** — deleting a step of a live chain cancels the
   chain first, so nothing reads a vanished input.
7. **Single live instance per chain** — rerun replaces a still-running chain
   instead of duplicating it.

## 11. Testing

```bash
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests -q
cd frontend && npm test          # vitest — the diff/table pure logic
```

`conftest.py` points `LOGS_DIR`/`DATA_DIR` at temp directories **before any
`app.*` import** (settings are read once at import), sets `LOGSMITH_TOKEN=""`,
pins `LOGSMITH_UI=python` (so the app under test serves the Jinja2 pages —
the `/api` routes are identical either way), and points `SCRIPTS_DIR` at the
repository's real sample scripts — the suite
runs actual `grep`/`python3` subprocesses, so it exercises the real execution
path. Executor-level tests fabricate `ScriptInfo` objects and patch the
executor's script lookup so advancement can be driven deterministically;
API tests drive one `TestClient` (full lifespan: reconcile → start → prune)
per test. CI (`.github/workflows/tests.yml`) installs the pinned
`requirements.lock`, runs the suite, builds the frontend and runs vitest on
every push and pull request; the repo is under git with `data/`,
`certs/`, `.env` and build outputs gitignored.

| Test module | Covers |
|---|---|
| `test_store.py` | persistence round-trip, newest-first listing, chain ordering, `reconcile()`, `prune()`, label persistence, delete-vs-save races |
| `test_executor.py` | single-job lifecycle: exit codes, timeouts, cancellation, gz/bz2/xz input decompression, script env vars, artifact collection, `chain_output()` |
| `test_chains.py` | invariants 1–4 + 6 below: artifact/stdout hand-off, compressed hand-off decompression, fail→skip, no-output→fail-next, gap tolerance, advancement-error containment, cancel-flag blocking |
| `test_chain_cancel.py` | invariant 5-adjacent cancel semantics: kill running + skip waiting, idempotent return values, skip reasons |
| `test_api.py` | the HTTP contract end-to-end: atomic validation (invariant 3), chain flow via real scripts, labels + `?q=` search (incl. chain ids), status filtering (single + comma-separated), pagination + post-filter totals, the `/input` endpoint (log, chained, decompressed, gone-source 404), the SSE stream (live stdout + terminal status frames), full/partial rerun (invariant 7) incl. 400/409 paths, delete-live-chain (invariant 6), cancel endpoint, prune guard |
| `test_scripts_api.py` | script editor endpoints against a temp SCRIPTS_DIR: create→discover→run, defaults, duplicate/bad-input rejection, source roundtrip, update incl. extension change, delete + rerun-404, export (contents + restore round-trip) |
| `test_job_store.py` | the parse cache: freshness after save, copy-on-return semantics, delete/wipe never leaking, corrupt job.json skipped |
| `test_preview.py` | log preview in head and tail modes, plain + compressed, the no-trailing-newline boundary, 50k-line file, 400/404/422 paths |
| `test_upload.py` | log upload: round-trip into the listing, duplicate 409, filename sanitization (traversal → basename), empty 422, 413 cap with partial cleanup, 403 when disabled |
| `test_pages.py` | the python UI page flow: home/run pages, form login + logout + Bearer, submission (single, fan-out, chain, unknown script 404, explicit boolean false), jobs list search/status/sort, cancel/delete/clear-finished, compare diff, upload + preview pages, the script editor (save, test-run, duplicate-safe ids, error re-render keeps values), the output table (sort, raw toggle, CSV incl. sorted) |
| `test_security.py` | the script authoring lock: API writes 403 (nothing lands on disk), reads/export/job submission unaffected, python UI editor pages bounce and hide actions, health reports the flag, editor works when unlocked |
| `test_auth.py` | the auth surface: 401 gate, Bearer + cookie login/logout (cookie never contains the token, carries a 7-day Max-Age), rate limiting, slate-clearing on success, no-op when auth is off |
| `frontend/src/logic.test.js` | vitest: `lineDiff` (LCS insert/delete/modify, empty sides, truncation) and `tableRows` (array-of-objects detection) — the UI's riskiest pure logic |

Invariant → test mapping: (1) cancel-flag blocking + `test_chain_cancel`,
(2) gap tolerance, (3) `test_api` atomic-validation cases, (4) decompression
tests in `test_executor` + `test_chains`, (5) TestClient per test exercising
lifespan shutdown, (6) delete-live-chain API test, (7) rerun-replaces +
partial-rerun tests in `test_api`.
