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
│                      (reconcile → prune → scheduler start; scheduler +
│                      executor + terminal shutdown on stop), and the
│                      frontend mount: the React SPA from frontend/dist
│                      or the python pages router, per LOGSMITH_UI
├── config.py          All settings, read from environment variables once
├── models.py          Pydantic models: ParamSpec, ScriptManifest, ScriptInfo
│                      (incl. the derived needs_log computed field), LogFile,
│                      JobSpecIn, JobCreateRequest, Job (incl. chain
│                      fields), JobStatus literal, PipelineStep/Pipeline,
│                      Schedule/ScheduleSaveRequest
├── auth.py            Shared-password auth: HttpOnly cookie session +
│                      Bearer token; constant-time comparisons; per-IP
│                      login rate limiting (5 failures/60s → 429);
│                      check_password/apply_session_cookie shared by the
│                      JSON API login and the HTML form login
├── pages.py           The python UI (LOGSMITH_UI=python): Jinja2 pages that
│                      call the routers' functions directly; templates in
│                      app/templates/, styles in app/static/, xterm.js
│                      (the terminal's) vendored in app/vendor/
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
│   └── pipelines.py   Saved pipelines: list / upsert-by-name / delete,
│                      validated at save time like a submission
│   └── schedules.py   Saved schedules: list (enriched with next fire) /
│                      upsert-by-name (cron + pipeline + log validated) /
│                      toggle / run-now (shares the ticker's fire path) /
│                      delete
│   └── terminal.py    Remote terminal endpoints (every one flag-gated
│                      to 404 while LOGSMITH_TERMINAL is off): create/
│                      kill pty sessions, SSE output stream, raw-body
│                      input, pty resize
└── services/
    ├── scanner.py     Discovers logs (recursive, newest first) and scripts
    │                  (flat + directory styles, default commands — which
    │                  include {log}, so a script is log-free only through
    │                  its manifest command)
    ├── job_store.py   Filesystem persistence; parse cache keyed by
    │                  (mtime, size); list_chain; reconcile; prune
    ├── pipeline_store.py  pipelines.json persistence: atomic rewrite,
    │                  lock-guarded read-modify-write, corrupt entries
    │                  skipped
    ├── schedule_store.py  schedules.json persistence: same shape as
    │                  pipeline_store, plus record_run (the ticker's
    │                  bookkeeping writes: last_run_at/last_result/
    │                  last_job_ids)
    ├── cron.py        The 5-field cron matcher: parse (Vixie semantics —
    │                  a/n steps, names, day/day-of-week OR rule),
    │                  matches(), next_after() (day-stepping, 4-year
    │                  window), describe() for the UIs; zero dependencies
    └── executor.py    Execution engine + chain (pipeline) engine
    └── scheduler.py   The ticker: one asyncio task, wakes every 30s,
                       fires due schedules through the normal submission
                       path; injectable clock for tests
    └── terminal.py    The remote terminal (LOGSMITH_TERMINAL): a pty
                       session registry — pty.fork() of $SHELL, reader
                       callback → bounded drop-oldest queue → SSE frames
                       (base64 JSON, like the job events stream), blocking-
                       safe input writes, idle reaper, whole-process-group
                       kill, and the python UI's one-shot run_command

backend/tests/         pytest suite: conftest isolates LOGS_DIR/DATA_DIR to
                       temp dirs before any app import; see §11

frontend/src/
├── App.jsx            State orchestration: auth, selection, ordered steps,
│                      parallel|chain mode, submission label, saved pipelines
│                      (server-side), schedules (cron-fired pipelines),
│                      job polling, notifications
├── api.js             Fetch wrapper; emits a global event on 401
├── presets.js         Per-script parameter presets (localStorage) + the
│                      one-time migration of legacy saved chains → server
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
    ├── SchedulesPanel.jsx  Schedules card: create form with cron preset
    │                      chips, per-schedule next fire / last result,
    │                      run-now / pause / delete
    ├── JobDetail.jsx      SSE live view, chain strip, markdown artifacts,
    │                      copy-as-command, re-run (full / from here)
    │                      ⇄ Compare (opens DiffView)
    └── DiffView.jsx       Unified line diff (hand-rolled LCS, 2000-line
                           cap) of two completed jobs' stdout

repo root
├── run.sh             Launcher: start/stop/status/restart (background or
│                      foreground), --ui python|react; bootstraps .venv,
│                      node_modules and the SPA build on demand; secure-by-
│                      default token generation
├── Dockerfile         Ships the whole app as one container (optional)
├── sandbox.Dockerfile Interpreter image for LOGSMITH_DOCKER_SANDBOX=1 runs
└── tools/
    ├── generate_cert.sh         Local TLS certificate for run.sh
    ├── generate_sample_logs.py  Sample data for a fresh checkout
    └── upload.sh                scp the git-tracked files to a remote host
                                (no .venv/node_modules/data/certs/.run/.env
                                — the remote bootstraps itself on first
                                start; ~/… dir targets expand on the remote,
                                --start restarts it so new code goes live)
```

## 3. Job execution engine (`services/executor.py`)

### 3.1 Single job lifecycle

1. **`submit(job, script)`** renders the final argv from the manifest command
   template (`{script}`, `{log}`, `{output}`, `{args}`) and persists it, then
   schedules `_run()` as an asyncio task. The rendered `command` is stored on
   the job record — the UI can show exactly what ran. Before scheduling, a
   pre-flight checks that the command can start: a missing interpreter (the
   command's own, or the one named in a directly executed script's shebang,
   including `#!/usr/bin/env X`) fails the job instantly with the same
   message the spawn error would give — a chain head takes its steps to
   `skipped`. Sandbox runs skip the pre-flight (their interpreters live in
   the sandbox image).
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
if neither            → no input at all (log-free run)
elif source is .gz/.bz2/.xz → read the shared cache copy (data/cache/decompressed/)
else                        → use the file directly
```

A **log-free run** (`log_path` empty, command without `{log}` — validated
at submission) has no input file: nothing is materialized, `input_path()`
returns `None`, the env carries no `JOB_LOG_PATH`/`JOB_ORIGINAL_LOG`, and
the sandbox mounts nothing but the script and the outputs dir. The `/input`
endpoint 404s and the UIs' Input tabs say so rather than pretending a file
was lost.

A compressed input is therefore **never** handed to a script raw — whether it
is the originally selected log or step N−1's gzipped artifact. The cache
keeps one decompressed copy per unique source version (path + size + mtime),
shared by every job and rerun, so a fan-out of N scripts on one `.gz` costs
one copy — not N. Entries carry a sidecar naming their source; startup and
manual prune drop the ones whose source is gone or changed.

### 3.3 Script runtime contract

Each script process receives `argv` from the rendered template, `cwd` = the
job's `outputs/` directory, and env vars `JOB_ID`, `JOB_LOG_PATH` (the
resolved, decompressed input), `JOB_OUTPUT_DIR`, and `JOB_ORIGINAL_LOG` (the
originally selected file — chain steps can still reach it). On a log-free
run the two log variables are absent — not empty. Artifacts are
whatever the script writes to `{output}` or into `$JOB_OUTPUT_DIR`.

With `LOGSMITH_DOCKER_SANDBOX=1` the same argv runs inside a `docker run
--rm` container instead: bind mounts keep the original paths, so template
rendering is untouched (see §9 for the isolation details).

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

### 4.8 Saved pipelines — stored chain configurations

A saved pipeline is a **named, validated snapshot of a chain
configuration** — ordered steps with their parameters — and nothing more.
Executing one is not a new code path: loading it just produces a normal
chain submission (§4.1), so the entire engine above is reused unchanged.

**Saving.** Both UIs save through the same validation and store:

- React runbar 💾 → `POST /api/pipelines` (`routers/pipelines.py`)
- python UI runbar 💾 → `POST /pipelines/save` (`pages.py`) — a
  `formaction` sibling of Run reusing the `/submit` form's fields, so the
  ordered scripts, mode and every `p::` parameter come along; on success
  it redirects to the same fully-configured `/run` URL

The router validates each step like a submission (`_validate_params`:
unknown script 404, bad params 422), so a stored pipeline is a typed
snapshot that always loads cleanly. `pipeline_store` persists it to
`data/pipelines.json` — lock-guarded read-modify-write, atomic tmp +
replace, the **name is the identity** (saving an existing name replaces
it, `created_at` kept), corrupt entries skipped on read.

**Loading.** A load replaces the current step selection (in order) and
forces chain mode; stored parameters prefill the forms:

- React 📂 replaces the drafted steps (`defaultParams` merged with the
  stored ones; steps whose scripts no longer exist are dropped with a
  toast).
- python UI `?pipeline=<name>` — the run page merges in the stored steps
  and parameters (stringified: booleans `true`/`false`, lists
  comma-joined — the same shapes the form submits). Scripts that no
  longer exist are skipped with an error flash. Explicit `p::` URL
  values still win over the stored ones, so a shared link can tweak one
  knob of a loaded pipeline.

**Sharing.** The python UI's `/run` carries parameter values as
`p::<script_id>::<param>` query params through Updates, category chips
and step reorders, so the URL of a configured run is complete on its
own — the save button redirects to exactly that URL.

**Migration.** The React UI's chains used to live in browser
localStorage. On first load after login, `migrateLegacyChains`
(`presets.js`) moves them to the server: names already saved there win, a
failed move keeps the local copy so the next load retries, and the
legacy key is removed only after every name is accounted for.

### 4.9 Scheduled runs — the cron ticker

A schedule is a saved pipeline (§4.8) — or its own script(s), stored as
inline `steps` — bound to a log (optional when the first step is
log-free, §3.2) and a cron expression, stored in `data/schedules.json`.
Firing one is not a new execution path: the ticker builds a normal
`JobCreateRequest` (mode `chain`, label = the schedule's name or its own
label) and awaits the same `create_jobs` the UIs call — so labels,
history, retention, `MAX_CONCURRENT_JOBS` and both UIs' job views all
apply to scheduled runs unchanged. A pipeline reference is resolved
fresh at fire time (editing the pipeline updates every schedule pointing
at it); inline steps are the schedule's own validated snapshot. Exactly
one of the two is set (XOR-checked at save), and both UIs create the
single-script case — `steps` of length one.

**The clock.** One asyncio task (`services/scheduler.py`), started by the
lifespan after the executor, sleeping 30s between ticks; each tick fires
every enabled schedule whose cron expression matches the current minute
in server-local time. Double-firing within a minute is guarded by the
persisted `last_run_at` (written on every fire, skip and error alike), so
a slow or duplicated tick can never run a schedule twice in one minute.
`LOGSMITH_SCHEDULER=0` leaves the task unstarted — schedules stay listed
and editable, and run-now still works.

**Cron matching** (`services/cron.py`, zero dependencies): the five
standard fields with Vixie semantics — `a/n` steps to the field maximum,
3-letter or full-prefix month/weekday names, 0 and 7 both Sunday, and
the day/day-of-week OR rule when both fields are restricted (a leading
`*`, including `*/n`, counts as unrestricted). `next_after()` steps
day-by-day rather than minute-by-minute, so a never-matching expression
is decided within a four-year window — and rejected at save time.

**Bounds, by design.** If any job of the previous fire is still
queued/waiting/running, the fire is skipped and recorded
(`skipped: previous run still active`) — never two of the same schedule
at once, never a queue of them. A minute lost to a restart is lost: no
catch-up, no retries, no backfill. A schedule that fails at fire time
(pipeline deleted, log gone, script removed) gets `error: …` recorded —
one schedule's failure never touches the ticker. Run-now (the ▶ button)
shares the exact same `fire()` path, so it obeys the same overlap rule.

**Why in-process.** Schedules are operator-sized (a handful), fire at
minute granularity, and only mean anything while the app runs — an
external cron daemon would add a dependency to save one 30-second loop.
When someone genuinely needs Airflow-grade guarantees, the bounded
surface is the point: export the schedule list and move on.

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
└── outputs/        # script cwd; artifacts served via /api/jobs/{id}/files/

data/cache/decompressed/                # shared decompressed copies (.gz/.bz2/.xz)
├── <key>.log       # one per unique source version (path + size + mtime)
└── <key>.json      # sidecar naming the source, for garbage collection

data/pipelines.json                      # saved pipelines: {name: Pipeline}
                                         # atomic rewrite on every save

data/schedules.json                      # saved schedules: {name: Schedule}
                                         # same rewrite discipline; the
                                         # ticker's record_run adds
                                         # last_run_at/last_result/last_job_ids
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
  `#/job/<id>` the job view, `#/terminal` the terminal tab (`useHashRoute` +
  `parseRoute` validating the job id shape). Refresh, browser back/forward
  and shareable job links all work without server-side routing.
- **TerminalPanel** (rendered only while `health.terminal` is on — the
  topbar link appears and disappears with it): one xterm.js instance per
  mount, `POST /api/terminal/sessions` to create, an EventSource on the
  output stream (base64 JSON frames; the exit frame closes it), keystrokes
  POSTed verbatim from `term.onData`, a debounced resize → `POST …/size`,
  and `DELETE` on unmount so closing the tab ends the shell. A "Start a new
  session" button restarts the whole cycle after an exit.
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
  localStorage — client-side only, unlike saved pipelines which live on
  the server and are shared with the python UI (browser notifications
  also have no python-UI equivalent).

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
- **The one JS-bearing page**: `/terminal` (while `LOGSMITH_TERMINAL` is on)
  mounts an interactive shell — xterm.js and the fit addon vendored in
  `app/vendor/` (byte-identical to the React build's copies, so no Node, no
  npm, no build step) and served flag-gated from `/terminal/assets/*` as an
  allowlist of four files. It is progressive enhancement: the page's command
  box stands in without JavaScript, and the client re-reveals it if the
  shell cannot start. Everything else stays plain HTML + forms.

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
- **Remote terminal** (`LOGSMITH_TERMINAL`, default off): an interactive
  shell is strictly more powerful than script authoring, so it is opt-in
  *and* subordinate to the lock — `TERMINAL_ENABLED` is the conjunction of
  the flag and `ALLOW_SCRIPT_EDITING`. While off, every endpoint 404s and
  both UIs hide it, so the feature does not exist to an attacker probing
  URLs. There is no sandbox path: the terminal is the trust boundary taken
  knowingly. Sessions are bounded (`LOGSMITH_TERMINAL_MAX_SESSIONS`), die
  with their tab, and are reaped after `LOGSMITH_TERMINAL_IDLE_TIMEOUT`
  without I/O; nothing a shell prints is persisted to disk. The transport
  is SSE + POST over the same auth middleware — a WebSocket would have
  bypassed it (the HTTP middleware does not intercept WS handshakes).
- **Docker sandbox** (`LOGSMITH_DOCKER_SANDBOX=1`): execution isolation
  for the run side of the trust boundary. The executor wraps the same argv
  in `docker run --rm --network none` with a memory cap, a pids limit, the
  host user's uid:gid and three bind mounts (script and input read-only at
  their original paths, the outputs dir read-write) — the command template
  needs no rewriting. The container sees only the `JOB_*` variables, so
  the server environment never leaks into scripts. Timeouts and cancels
  kill the container by name, not just the docker CLI. There is no silent
  fallback: if Docker is unavailable, jobs fail with an explicit error.
- Trust boundary: without the sandbox, scripts are **arbitrary code running
  as the server user** — a trusted-operator tool, not multi-tenant. With
  the sandbox on, scripts can only read their input and write their
  outputs. Run the server under a dedicated non-admin account when in
  doubt.

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
| `test_executor.py` | single-job lifecycle: exit codes, timeouts, cancellation, gz/bz2/xz input decompression, script env vars, log-free runs (no env vars, no input), artifact collection, `chain_output()` |
| `test_chains.py` | invariants 1–4 + 6 below: artifact/stdout hand-off, compressed hand-off decompression, fail→skip, no-output→fail-next, gap tolerance, advancement-error containment, cancel-flag blocking |
| `test_chain_cancel.py` | invariant 5-adjacent cancel semantics: kill running + skip waiting, idempotent return values, skip reasons |
| `test_api.py` | the HTTP contract end-to-end: atomic validation (invariant 3), chain flow via real scripts, `needs_log` derivation, log-free submissions (ok, 422 named rejections, chain step-1 rule, rerun), labels + `?q=` search (incl. chain ids), status filtering (single + comma-separated), pagination + post-filter totals, the `/input` endpoint (log, chained, decompressed, gone-source 404), the SSE stream (live stdout + terminal status frames), full/partial rerun (invariant 7) incl. 400/409 paths, delete-live-chain (invariant 6), cancel endpoint, prune guard; parallel jobs must overlap in wall-clock while the API keeps answering mid-run (the async contract) |
| `test_scripts_api.py` | script editor endpoints against a temp SCRIPTS_DIR: create→discover→run, defaults, duplicate/bad-input rejection, source roundtrip, update incl. extension change, delete + rerun-404, export (contents + restore round-trip); scripts written executable (755) so `{script}`-direct commands run, and spawn failures carry hints (EACCES/ENOEXEC/ENOENT) |
| `test_job_store.py` | the parse cache: freshness after save, copy-on-return semantics, delete/wipe never leaking, corrupt job.json skipped |
| `test_preview.py` | log preview in head and tail modes, plain + compressed, the no-trailing-newline boundary, 50k-line file, 400/404/422 paths |
| `test_upload.py` | log upload: round-trip into the listing, duplicate 409, filename sanitization (traversal → basename), empty 422, 413 cap with partial cleanup, 403 when disabled |
| `test_pages.py` | the python UI page flow: home/run pages, form login + logout + Bearer, submission (single, fan-out, chain, unknown script 404, explicit boolean false, log-free incl. the no-log run page + 422), jobs list search/status/sort, cancel/delete/clear-finished, compare diff, upload + preview pages, the script editor (save, test-run, duplicate-safe ids, error re-render keeps values), the output table (sort, raw toggle, CSV incl. sorted), saved pipelines (runbar save → shareable redirect URL, `?pipeline=` load prefills steps + params, `p::` values surviving Update and reorder links, unknown-name flash, Load-card delete + 404), schedules (page + create form + flash, validation rendering, `?schedule=` edit prefill and `?cron=` preset prefill, toggle/run/delete, script mode: wizard prefill, fieldset, save/run, needs-log rejection, no-pipelines form) |
| `test_security.py` | the script authoring lock: API writes 403 (nothing lands on disk), reads/export/job submission unaffected, python UI editor pages bounce and hide actions, health reports the flag, editor works when unlocked |
| `test_sandbox.py` | the Docker sandbox: the `docker run` wrapper's shape (mounts, no network, uid:gid, JOB_* env, container name, image + command order), compressed-input mounts, log-free shape (no input mount, no JOB_LOG_* env); e2e (skipped without Docker + image): completion, artifacts owned by the host user, no network, timeout kills the container, every interpreter |
| `test_auth.py` | the auth surface: 401 gate, Bearer + cookie login/logout (cookie never contains the token, carries a 7-day Max-Age), rate limiting, slate-clearing on success, no-op when auth is off |
| `test_pipelines_api.py` | the saved-pipeline API: save/list/delete round-trip, upsert-by-name (created_at kept), validation (unknown script 404, bad params 422, empty name/steps), delete 404 |
| `test_cron.py` | the cron matcher: next-fire for every construct (steps, ranges, lists, names, Vixie `a/n`, the day OR rule, Sunday 0/7, leap day), strictly-after semantics, rejection table (field count, ranges, Feb 30, zero steps), `describe()` labels |
| `test_scheduler.py` | the ticker engine with an injected clock: fires once per matching minute, no double-fire within a minute, no catch-up for minutes lost while down, disabled never fires, overlap skip (recorded, nothing submitted), finished previous run doesn't block, inline steps fire as a chain (log-free), neither-pipeline-nor-steps error, missing pipeline / submission errors recorded not raised, multiple schedules in one tick |
| `test_schedules_api.py` | the schedule API: save/list round-trip with next_due + cron_desc + target, upsert keeps bookkeeping, validation (cron 422 incl. never-matching, pipeline/log/script 404, escape 400, broken pipeline 404, pipeline XOR steps 422, log-required-iff-first-step-needs-it), inline-steps save/toggle/run-now, toggle/delete + 404s, run-now submits a real chain labelled with the schedule's name, custom label, missing log recorded as `error:` |
| `test_terminal.py` | the remote terminal: flag off = hidden (404s everywhere, no nav link, health false), the lockdown conjunction (`LOGSMITH_ALLOW_SCRIPT_EDITING=0` forces it off at config level), full session lifecycle over the SSE protocol (create → typed command → stream: output frames + one exit frame with the code), unknown ids, resize bounds, kill + 404, 413 over-long input, 410 after exit, the session cap (429), output queuing across a dropped connection, the one-shot `run_command` (ok / nonzero / timeout), and the python UI page (nav link, run, blank-input flash, the interactive-shell markup with the command box as fallback, vendored assets serving + traversal/allowlist + hidden while off) |
| `frontend/src/logic.test.js` | vitest: `lineDiff` (LCS insert/delete/modify, empty sides, truncation) and `tableRows` (array-of-objects detection) — the UI's riskiest pure logic |

Invariant → test mapping: (1) cancel-flag blocking + `test_chain_cancel`,
(2) gap tolerance, (3) `test_api` atomic-validation cases, (4) decompression
tests in `test_executor` + `test_chains`, (5) TestClient per test exercising
lifespan shutdown, (6) delete-live-chain API test, (7) rerun-replaces +
partial-rerun tests in `test_api`.
