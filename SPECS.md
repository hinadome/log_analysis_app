# LogSmith Specification

Normative functional specification. Implementation details live in
[ARCHITECTURE.md](ARCHITECTURE.md); setup and usage in [README.md](README.md).

## 1. Concepts

| Concept | Definition |
|---|---|
| **Log** | Any file under `LOGS_DIR` (recursive, hidden entries skipped), listed newest-first. `.gz`/`.bz2`/`.xz` logs are supported and decompressed transparently. |
| **Script** | An executable under `SCRIPTS_DIR` plus its manifest (or a default command derived from its extension). See §6. |
| **Job** | One execution of one script against one input. Fully persisted; identified by `YYYYMMDD-HHMMSS-<8hex>`. |
| **Chain** | A submission of ≥2 jobs in `mode: "chain"` that run sequentially, each step's input being the previous step's output. |
| **Artifact** | A file a script writes into its `outputs/` directory (via `{output}` or `$JOB_OUTPUT_DIR`). |

## 2. Job statuses

| Status | Meaning | Terminal? |
|---|---|---|
| `queued` | Accepted; waiting for a pool slot (task scheduled) | no |
| `waiting` | Chain step whose predecessor has not finished (no task exists) | no |
| `running` | Process spawned, output streaming | no |
| `completed` | Exited with code 0 | yes |
| `failed` | Exit ≠ 0, timeout, spawn error, decompress error, chain starvation, or interrupted by restart | yes |
| `cancelled` | Killed by user request or by server shutdown (while running) | yes |
| `skipped` | Chain step that never ran because an earlier step did not complete | yes |

Standard error strings: `Cancelled by user`, `Cancelled by server shutdown`,
`Interrupted by server restart`, `Skipped: step N did not complete`,
`Previous step produced no output to chain`, `Script no longer available:
<id>`, `Timed out after N seconds`, `Exited with code N`, `Failed to start:
…`, `Failed to decompress input: …`.

## 3. Job record schema

Returned by `POST /api/jobs` (as a list), `GET /api/jobs`, `GET /api/jobs/{id}`:

| Field | Type | Notes |
|---|---|---|
| `id` | string | `YYYYMMDD-HHMMSS-<8 hex>` |
| `script_id` / `script_name` | string | manifest id / display name |
| `log_path` / `log_name` | string | originally selected log (absolute / base name) |
| `label` | string\|null | user tag for the submission this job belongs to |
| `params` | object | validated parameter values |
| `command` | string[] | final argv as executed |
| `status` | string | see §2 |
| `created_at` / `started_at` / `finished_at` | string\|null | ISO-8601 UTC |
| `exit_code` | int\|null | |
| `error` | string\|null | human-readable reason |
| `stdout_size` / `stderr_size` | int | bytes |
| `output_files` | string[] | artifact file names |
| `chain_id` | string\|null | set for chain steps |
| `chain_index` / `chain_size` | int | 0-based position / total steps |
| `input_source` | string\|null | `log` \| `prev_output` \| `prev_stdout` |
| `input_path` | string\|null | the file `{log}` resolved to |
| `output_file` | string\|null | `{output}` target, when declared |

`GET /api/jobs/{id}` additionally returns `stdout_tail` / `stderr_tail`
(last 32 KB by default), and for chain steps a `chain_steps` array:
`[{id, index, script_name, status, error}, …]` ordered by step index.

## 4. Submission modes

`POST /api/jobs` body:

```json
{
  "log_path": "/abs/or/relative/path.log",
  "mode": "parallel" | "chain",
  "label": "db-error investigation",
  "jobs": [{"script_id": "grep_search", "params": {"pattern": "x"}}]
}
```

- `mode` defaults to `parallel`. `jobs` must be non-empty (422 otherwise).
- `label` is optional, trimmed to 80 characters, and stored on every job of
  the submission; it is searchable via `GET /api/jobs?q=` and shown in the UI.
- **Validation is atomic (two-pass)**: every step's script existence, manifest
  health and parameter types are checked **before** any job is created. A
  rejected request (404 unknown script / 422 bad params) leaves zero jobs.
- `parallel`: all jobs start immediately (bounded by the pool); each reads
  the selected log. A single job in `chain` mode behaves identically.

### 4.1 Chain rules (normative)

1. Step 1 reads the selected log. Step N>1 reads step N−1's output.
2. **Input resolution** for the next step, in order: the previous step's
   declared artifact (`output_filename` / `{output}`), if it exists → else
   the single file in its `outputs/` directory (only when exactly one) →
   else its `stdout.txt` (only when non-empty). If none exists, the next
   step fails with `Previous step produced no output to chain`.
3. Compressed inputs (`.gz`/`.bz2`/`.xz` — the selected log **or** a chained
   output) are decompressed into the step's own `input.log` before the
   script runs; scripts always receive a plain path.
4. Commands for steps 2+ are rendered lazily at start time (the input file
   does not exist before then).
5. A step that ends `failed` or `cancelled` stops the chain; every later
   `waiting` step becomes `skipped`.
6. **Cancelling any step cancels the whole chain** (active step killed,
   waiting steps skipped) — atomically, including completion races.
7. **Re-running any step re-runs the whole chain** with the original specs
   (§4.2 for the partial variant); if the old chain is still live it is
   cancelled first (replaces, never duplicates). The submission's `label`
   is propagated to the re-run.
8. **Deleting a step of a still-live chain cancels the chain first**;
   deleting steps of a finished chain removes them individually.
9. The same script may appear multiple times in one chain.
10. Every step's process receives `JOB_ORIGINAL_LOG` pointing at the
    originally selected file.

### 4.2 Partial re-run (continue from step N)

`POST /api/jobs/{id}/rerun?from_step=N` (1-based, N ≥ 2) re-runs only step N
and the ones after it:

- The new chain **preserves the original step numbering** — re-running from
  step 2 of a 5-step chain yields `chain_index` 1..4 with `chain_size` 5, so
  the UI still shows "step 2/5".
- Step N's input is seeded from the **nearest preceding surviving step's
  persisted output** (per §4.1 rule 2). If that step is not `completed`, the
  request fails with 409 — a partial rerun never re-executes earlier steps
  and never chains off an unverified input; rerun the full chain instead.
- If all preceding steps were deleted, the new chain starts from the
  selected log.
- Errors: 400 when `from_step` exceeds the chain size or the step no longer
  exists, 409 when the preceding surviving step did not complete (or
  produced no output). Validation is atomic as everywhere else; the old
  chain is only cancelled after the request is fully validated.

## 5. API reference

All endpoints are under `/api`, require auth when `LOGSMITH_TOKEN` is set
(§8), and return JSON unless noted. Errors use FastAPI's
`{"detail": "…"}` shape.

### Auth

| Method & Path | Description |
|---|---|
| `GET /api/auth/status` | `{"auth_required": bool}` |
| `POST /api/auth/login` | `{"password": "…"}` → sets HttpOnly cookie; 401 on wrong password; **rate-limited: 5 failed attempts per client IP per 60 s → 429** with `Retry-After` (a successful login clears the counter); no-op success when auth disabled |
| `POST /api/auth/logout` | clears the session cookie |

### Logs

| Method & Path | Description |
|---|---|
| `GET /api/logs` | `{"logs": [{name, path, size, modified}]}` — recursive, newest first |
| `GET /api/logs/preview?path=…&lines=200[&tail=1]` | `lines` ≤ 2000; decompresses on the fly → `{name, path, size, compressed, lines[], truncated}`. Default reads the first lines; `tail=1` reads the last lines (backwards seek for plain files, streamed window for compressed ones) |
| `POST /api/logs/upload` | multipart `file` field → saves it into `LOGS_DIR` (flat) → `{saved, size, log}`. The name is reduced to a safe basename (`^[\w][\w.\- ]{0,254}$`); errors: 409 name exists / 413 over `MAX_LOG_UPLOAD_MB` / 422 empty or invalid name / 403 uploads disabled. Written as a hidden `.name.part` until complete, so partial uploads never appear in listings |

### Scripts

| Method & Path | Description |
|---|---|
| `GET /api/scripts` | `{"scripts": [ScriptInfo]}` — id, name, description, command, timeout, parameters, script_path, style, output_filename, error |
| `GET /api/scripts/{id}` | code + manifest for the editor (flat style only) → `{id, extension, code, manifest}`; 404 unknown, 400 directory-style |
| `POST /api/scripts` | create a flat-style script. Body: `{id, extension, code, manifest}` (`manifest` per §6). Errors: 409 id exists / 422 bad id (`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`), empty code, bad timeout (1–86400), or invalid manifest/parameters / 403 when editing is locked (§8.1) |
| `PUT /api/scripts/{id}` | update code and/or manifest; a changed `extension` moves the code file. 404 unknown, 400 directory-style, 422 body/path id mismatch, 403 when editing is locked |
| `DELETE /api/scripts/{id}` | removes the code file and its sidecar. Job history is kept (stdout/artifacts are per-job); jobs of a deleted script can no longer be re-run (404). 403 when editing is locked |
| `GET /api/scripts/{id}/export` | tar.gz download: code + sidecar manifest (flat) or the full directory (directory-style), plus a `RESTORE.txt` explaining where to unpack |

### Jobs

| Method & Path | Description |
|---|---|
| `POST /api/jobs` | submission (§4) → `{"jobs": [Job]}`. Errors: 404 unknown script / 404 unknown log / 400 path escapes `LOGS_DIR` / 422 bad params or empty list |
| `GET /api/jobs?status=…&q=…&limit=&offset=` | list jobs (newest first), optionally filtered by status (single value or comma-separated, e.g. `queued,waiting,running` for "active") and/or free-text `q` (matches label, script name/id, log name, job id, chain id). Paged: `limit` ≤ 5000 (default 200), `offset` ≥ 0 → `{jobs, total}` with `total` post-filter |
| `GET /api/jobs/{id}` | job detail incl. tails and `chain_steps` (§3) |
| `GET /api/jobs/{id}/stdout` `/stderr` | full text (`text/plain`, 20 MB cap) |
| `GET /api/jobs/{id}/input` | the exact input this job's script received — the selected log, or for chain steps the previous step's output (compressed sources served decompressed). `text/plain`, 20 MB cap; 404 when the source file is gone |
| `GET /api/jobs/{id}/files/{name}` | download an artifact (name must be a single path segment) |
| `GET /api/jobs/{id}/events` | SSE: `{"type":"status", status, error, exit_code, started_at, finished_at, stdout_size, stderr_size, output_files, stderr_tail}` and `{"type":"stdout","text"}` deltas; ends with `{"type":"gone"}` if the job is deleted. Stays open while `queued`/`running`/`waiting` |
| `POST /api/jobs/{id}/cancel` | chain job → cancels whole chain; single job → kills it. 409 if nothing active |
| `POST /api/jobs/{id}/rerun` | new job / new chain with the same config → `{"job": …}`; chains accept `?from_step=N` (§4.2). Errors: 404 if a script no longer exists / 400 bad `from_step` / 409 unseedable predecessor |
| `DELETE /api/jobs/{id}` | cancels a live chain first when applicable, then deletes the record |
| `POST /api/jobs/prune?days=N` | deletes job records older than N days (1–3650) → `{"removed", "retention_days"}`; 400 when retention is disabled and no `days` given |

### Meta

| Method & Path | Description |
|---|---|
| `GET /api/health` | `{status, logs_dir, scripts_dir, data_dir, max_concurrent_jobs, auth_required, script_editing, job_retention_days, data_dir_size}` |

Interactive OpenAPI docs: `http://localhost:8000/docs`.

### 5.1 Pages (python UI)

When `LOGSMITH_UI=python`, the backend serves server-rendered HTML pages
instead of the React SPA (`app/pages.py`; the two are mutually exclusive —
the SPA owns a catch-all route). Pages call the same handlers as the API,
share its session cookie, and redirect to `/login?next=…` when a session is
missing.

| Path | Description |
|---|---|
| `GET /` | home: log list, script checkboxes, upload form |
| `GET /run?log=…&logs=…&scripts=…&mode=…&label=…` | run configuration — a shareable GET; renders one parameter form per selected script (fields named `p::<script_id>::<param>`; booleans = hidden `false` + checkbox `true`; multi = comma-separated; empty = manifest default) |
| `POST /submit` | form submission: one submission per selected log (fan-out), parallel or chain; single job → 303 to its page, else 303 to the jobs list |
| `GET /jobs?q=&status=&sort=&dir=&offset=` | job history — server-side search, status chips, sortable columns, paging (50/page) |
| `GET /jobs/{id}?tab=result\|stderr\|input\|files\|details` | job detail. The result tab of a finished job renders stdout like the React OutputView: JSON array of objects → sortable table (`?sortcol=&sortdir=` cycles asc → desc → natural; `?view=raw` toggles the raw text), other JSON → pretty-printed, text → plain. Active jobs tail live via a 2 s meta-refresh |
| `GET /jobs/{id}/result.csv?sortcol=&sortdir=` | the table view as CSV (`output.csv`), sorted as shown; 422 when the output is not a JSON table |
| `GET /jobs/{id}/compare[?other=]` | unified line diff of two completed jobs' stdout (difflib, 2000 lines/side); defaults to the latest run of the same script |
| `POST /jobs/{id}/cancel` `/delete` `/rerun[?from_step]`, `POST /jobs/clear-finished`, `POST /jobs/prune` | form-backed actions; each redirects back (303) |
| `GET /logs/preview?path=&lines=&tail=1` | head/tail log preview page |
| `GET /scripts`, `/scripts/new`, `/scripts/{id}/edit` | script list and editor |
| `POST /scripts/save`, `/scripts/test-run` (log chosen in the form; runs with manifest defaults), `/scripts/{id}/delete` | editor actions; an id different from the edited one saves as a new script |
| `GET /login` `POST /login` `POST /logout` | HTML form login against the same rate-limited password check; logout clears the cookie |
| `GET /static/*` | the UI stylesheet (mounted before the pages router) |
| anything else | an HTML 404 page |

## 6. Script manifest specification

Scripts are discovered in two shapes:

1. **Flat** — `scripts/name.py` + sidecar `scripts/name.json`.
2. **Directory** — `scripts/name/manifest.json` + entry file (named via
   `entry`, or the first non-hidden file that is not the manifest).

Without a manifest, `.py/.sh/.bash/.zsh/.js/.rb/.pl` files get a default
command `<interpreter> {script} {log} {args}`; other files are ignored. A
broken manifest surfaces the script with an `error` field instead of hiding
it (and submitting it is rejected with 422 — but it can be fixed in the UI
editor, which rewrites a clean manifest on save).

Scripts can also be authored through the API/UI editor (`POST/PUT/DELETE
/api/scripts`): it writes ordinary flat-style files (code + sidecar
manifest), so UI-created and hand-added scripts are interchangeable.
Directory-style scripts are read-only in the UI.

```jsonc
{
  "name": "Error Counter",          // UI display name (defaults to id)
  "description": "Count lines by level",
  "command": "python3 {script} {log} {args}",  // template, see below
  "timeout": 120,                   // seconds; killed after (default 600)
  "output_filename": "report.md",   // optional; target of {output}
  "entry": "run.py",                // directory style only
  "parameters": [
    {
      "name": "level",              // key used by UI/API params
      "flag": "--level",            // CLI flag rendered into {args}
      "label": "Severity",          // optional friendlier UI label
      "type": "enum",              // string|integer|float|boolean|enum|multi
      "choices": ["ERROR", "WARN"], // enum only
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
| `{log}` | absolute path of the input — the selected log, or in a chain the previous step's output (decompressed copy for `.gz`/`.bz2`/`.xz`) |
| `{output}` | `<job>/outputs/<output_filename>` — set when the command contains it |
| `{args}` | rendered CLI flags from validated parameters |

Templates are tokenized with shell rules but executed **without a shell**
(exec), so parameter values are never interpreted as shell syntax.

### Parameter types and rendering

| Type | UI control | Rendering into `{args}` | Validation |
|---|---|---|---|
| `string` | text | `--flag value` | — |
| `integer` | number | `--flag value` | must parse as int |
| `float` | number | `--flag value` | must parse as float |
| `boolean` | checkbox | `--flag` only when true | must be bool |
| `enum` | select | `--flag value` | value ∈ `choices` |
| `multi` | multi-select | `--flag v1 --flag v2` | list (or single string) |

Unknown parameter names are rejected (422). Missing required parameters are
rejected (422). Empty/false values are omitted from `{args}`.

### Script runtime contract

Each script process receives:

- `argv` rendered from the command template
- `cwd` = the job's `outputs/` directory
- env: `JOB_ID`, `JOB_LOG_PATH` (resolved input path), `JOB_OUTPUT_DIR`,
  `JOB_ORIGINAL_LOG` (the originally selected file) + the server environment

Artifacts: write to the `{output}` path, or write any files into
`$JOB_OUTPUT_DIR` — both are served by `GET /api/jobs/{id}/files/{name}`.
In a chain, whatever the step leaves there (or its stdout) feeds the next
step per §4.1 rule 2.

## 7. Retention

- `JOB_RETENTION_DAYS` (default `0` = disabled) auto-deletes job records
  older than N days **at startup only** — never while running.
- Manual pruning: `POST /api/jobs/prune?days=N` (requires `days` when
  retention is disabled) and the 🧹 button in the Jobs panel.

## 8. Authentication

- `LOGSMITH_TOKEN` set → every `/api/*` route except `/api/auth/*` requires
  authentication. Two accepted credentials:
  - session cookie `logsmith_session` (HttpOnly, SameSite=Lax, Secure on
    HTTPS) obtained via `POST /api/auth/login` (the python UI posts the same
    check from its HTML form, then guards pages with a redirect to
    `/login?next=…`)
  - `Authorization: Bearer <LOGSMITH_TOKEN>` for automation
- `LOGSMITH_TOKEN` unset → auth disabled, and the launcher is secure by
  default: a non-local bind generates a strong token into `.run/authtoken`
  (printed once, reused across restarts) instead of starting open.
  `LOGSMITH_INSECURE=1` opts out; a local bind (`LOGSMITH_HOST=127.0.0.1`)
  skips auth entirely.
- The SPA serves without a token; API calls failing with 401 raise a global
  event that switches the UI to the login screen.

### 8.1 Script authoring lock

Scripts are arbitrary code running as the server user, so the editor is
remote code execution by design. `LOGSMITH_ALLOW_SCRIPT_EDITING=0` narrows
that trust: `POST /api/scripts`, `PUT` and `DELETE /api/scripts/{id}` answer
**403** (the python UI also bounces its editor pages and hides the editor
actions), while listing, source view, export and job submission keep
working. `/api/health` reports the state as `"script_editing": bool` so the
UIs can adapt.

## 9. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LOGS_DIR` | `./logs` | where log files are listed from |
| `SCRIPTS_DIR` | `./scripts` | where scripts/manifests are discovered |
| `DATA_DIR` | `./data` | job records, stdout/stderr, artifacts |
| `MAX_CONCURRENT_JOBS` | `4` | process pool size (extra jobs stay `queued`) |
| `DEFAULT_TIMEOUT` | `600` | fallback timeout seconds (manifest wins) |
| `LOG_PREVIEW_LINES` | `200` | default lines in log preview |
| `STDOUT_TAIL_BYTES` | `32768` | stdout/stderr bytes inlined in job detail |
| `MAX_LOG_UPLOAD_MB` | `512` | max size of a browser log upload (0 disables uploads) |
| `LOGSMITH_TOKEN` | *(unset)* | UI password; when unset and the bind is non-local, the launcher generates one into `.run/authtoken` |
| `LOGSMITH_INSECURE` | `0` | launcher escape hatch: `1` = non-local bind with no password (not recommended) |
| `LOGSMITH_ALLOW_SCRIPT_EDITING` | `1` | `0` = script write endpoints answer 403 (§8.1); existing scripts stay runnable |
| `LOGSMITH_HOST` | `0.0.0.0` | bind address (launcher-level; `127.0.0.1` = local only) |
| `LOGSMITH_UI` | `react` | which frontend the backend serves: `react` (built SPA) or `python` (server-rendered pages, §5.1); same as `run.sh --ui` |
| `LOGSMITH_CERT_HOSTS` | *(unset)* | extra TLS SANs for the launcher's cert generation |
| `JOB_RETENTION_DAYS` | `0` | startup auto-delete window (0 = keep forever) |

All variables are read once at process start; restart to apply changes.
