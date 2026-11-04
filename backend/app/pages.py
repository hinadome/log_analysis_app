"""Server-rendered frontend (the "python" UI).

Jinja2 pages that replicate the React SPA without a build step: the whole
workbench — log picking, uploads, parameter configuration, job monitoring,
script editing — renders on the server, so every view is a plain,
deep-linkable URL. Mounted instead of the SPA when LOGSMITH_UI=python
(see main.py and `run.sh --ui python`).

The pages call the API routers' functions directly (no HTTP round-trip)
and share their session cookie, so both frontends have one auth story.
"""
from __future__ import annotations

import csv
import difflib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import auth
from .config import (LOG_PREVIEW_LINES, MAX_LOG_UPLOAD_MB, ALLOW_SCRIPT_EDITING,
                     DOCKER_SANDBOX, TERMINAL_ENABLED, LOGS_DIR)
from .models import (
    Job, JobCreateRequest, JobSpecIn, PipelineSaveRequest, PipelineStep,
    ScheduleSaveRequest, ScriptManifest, ScriptUpsertRequest,
)
from .routers import jobs as jobs_api
from .routers import logs as logs_api
from .routers import pipelines as pipelines_api
from .routers import schedules as schedules_api
from .routers import scripts as scripts_api
from .routers import users as users_api
from .services import pipeline_store
from .services import script_access
from .services.scanner import list_logs
from .services.terminal import terminal as terminal_service

router = APIRouter(include_in_schema=False)
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ACTIVE = ("queued", "waiting", "running")
PAGE_SIZE = 50
MAX_DIFF_LINES = 2000  # per side, like the React DiffView
EXTENSIONS = ["py", "sh", "bash", "zsh", "js", "rb", "pl"]

STATUS_FILTERS = [
    {"value": "", "label": "All"},
    {"value": "queued,waiting,running", "label": "Active"},
    {"value": "completed", "label": "Completed"},
    {"value": "failed", "label": "Failed"},
    {"value": "cancelled,skipped", "label": "Cancelled"},
]

# /jobs table columns that can be sorted server-side (?sort=&dir=)
_SORT_KEYS = {
    "created": lambda j: j.created_at,
    "script": lambda j: j.script_name.lower(),
    "log": lambda j: j.log_name.lower(),
    "status": lambda j: j.status,
    "label": lambda j: (j.label or "").lower(),
}

STARTER_CODE = (
    "#!/usr/bin/env python3\n"
    "import sys\n"
    "\n"
    "# The manifest command receives {log} as the input file path;\n"
    "# reading stdin works too when the command pipes the log in.\n"
    "for line in sys.stdin:\n"
    '    print(line, end="")\n'
)


# --- template filters ----------------------------------------------------

def _fmt_dt(value: str | None) -> str:
    """'2026-10-29T10:00:05.123+00:00' -> '2026-10-29 10:00:05'."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _fmt_bytes(value: int | float | None) -> str:
    n = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration(job: Job) -> str:
    start = _parse_ts(job.started_at)
    if not start:
        return ""
    end = _parse_ts(job.finished_at) or datetime.now(timezone.utc)
    secs = max(0.0, (end - start).total_seconds())
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(int(secs), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["kb"] = _fmt_bytes


# --- shared helpers ------------------------------------------------------

def _need_login(request: Request) -> RedirectResponse | None:
    """Redirect to the login page when auth is on and this client lacks it."""
    if not auth.auth_required() or auth.is_authenticated(request):
        return None
    return RedirectResponse("/login?" + urlencode({"next": request.url.path}), status_code=303)


def _safe_next(target: str | None) -> str:
    """Only same-site absolute paths — never a protocol-relative URL."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


def _page(request: Request, name: str, status_code: int = 200, **ctx):
    current = auth.current_user(request)
    context = {
        "auth_required": auth.auth_required(),
        "multi_user": auth.multi_user_enabled(),
        "current_user": current,
        # admins see the Users page; in single-user mode the local operator
        # is the admin (that's how the first account gets bootstrapped)
        "is_admin": (current is not None and current.role == "admin")
                   or not auth.multi_user_enabled(),
        "docker_sandbox": DOCKER_SANDBOX,
        "terminal_enabled": TERMINAL_ENABLED,
        "logs_dir": str(LOGS_DIR),
        "msg": request.query_params.get("msg", ""),
        "err": request.query_params.get("err", ""),
    }
    context.update(ctx)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _render_error(request: Request, exc: HTTPException, back: str = "/"):
    return _page(request, "error.html", status_code=exc.status_code,
                 status=exc.status_code, detail=str(exc.detail), back=_safe_next(back))


def _selected_logs(single: str, many: list[str] | None) -> list[str]:
    """Logs chosen via ?log= (single Run link) or ?logs= (checkbox fan-out)."""
    names = [single] if single else []
    names += list(many or [])
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def _line_diff(a_text: str, b_text: str) -> dict:
    """Line diff for the compare view (difflib, capped per side)."""
    full_a, full_b = a_text.split("\n"), b_text.split("\n")
    truncated = len(full_a) > MAX_DIFF_LINES or len(full_b) > MAX_DIFF_LINES
    a = full_a[:MAX_DIFF_LINES]
    b = full_b[:MAX_DIFF_LINES]
    if a and a[-1] == "":
        a.pop()
    if b and b[-1] == "":
        b.pop()

    rows: list[dict] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows.extend({"type": "same", "text": line} for line in a[i1:i2])
        else:
            if tag in ("replace", "delete"):
                rows.extend({"type": "del", "text": line} for line in a[i1:i2])
            if tag in ("replace", "insert"):
                rows.extend({"type": "add", "text": line} for line in b[j1:j2])
    return {
        "rows": rows,
        "truncated": truncated,
        "added": sum(1 for r in rows if r["type"] == "add"),
        "removed": sum(1 for r in rows if r["type"] == "del"),
    }


def _cell_sort_key(value):
    """Total ordering for table cells: numbers numeric, then strings, nulls last."""
    if value is None:
        return (2, 0.0, "")
    if isinstance(value, bool):
        return (1, 0.0, str(value).lower())
    if isinstance(value, (int, float)):
        return (0, value, "")
    return (1, 0.0, str(value))


def _output_view(text: str, view: str, sortcol: str, sortdir: int) -> dict:
    """Interpret finished job stdout the way the React OutputView does:
    an array of flat objects becomes a sortable table, other JSON is
    pretty-printed, anything else stays plain text. `view` = "raw" forces
    the raw text for table output (the Table/Raw toggle).
    """
    trimmed = (text or "").strip()
    parsed = None
    if trimmed.startswith("{") or trimmed.startswith("["):
        try:
            parsed = json.loads(trimmed)
        except ValueError:
            parsed = None

    if (isinstance(parsed, list) and parsed
            and all(isinstance(r, dict) for r in parsed)):
        columns = list(dict.fromkeys(c for r in parsed for c in r))
        rows = list(parsed)
        if sortcol in columns:
            rows = sorted(rows, key=lambda r: _cell_sort_key(r.get(sortcol)),
                          reverse=sortdir == -1)
        return {"kind": "table", "view": "raw" if view == "raw" else "table",
                "text": text, "columns": columns, "rows": rows,
                "sortcol": sortcol if sortcol in columns else "",
                "sortdir": -1 if sortdir == -1 else 1}
    if parsed is not None:
        return {"kind": "json", "view": "", "text": text,
                "pretty": json.dumps(parsed, indent=2)}
    return {"kind": "text", "view": "", "text": text}


def _csv_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


# --- login / logout ------------------------------------------------------

@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if not auth.auth_required() or auth.is_authenticated(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _page(request, "login.html", next=_safe_next(next), error="")


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    target = _safe_next(form.get("next"))
    if not auth.auth_required():
        return RedirectResponse(target, status_code=303)
    try:
        value = auth.check_login(request, form.get("username") or "",
                                 form.get("password") or "")
    except HTTPException as exc:
        return _page(request, "login.html", status_code=exc.status_code,
                     next=target, error=str(exc.detail))
    response = RedirectResponse(target, status_code=303)
    if value:
        auth.apply_session_cookie(request, response, value)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


# --- home: logs, upload, script picker -----------------------------------

@router.get("/")
def home(request: Request, cat: str = ""):
    if redirect := _need_login(request):
        return redirect
    scripts = script_access.visible_scripts(auth.current_user(request))
    categories = sorted({s.category for s in scripts if s.category})
    # A hand-edited ?cat= that no script carries shows everything, not an
    # empty table that looks like a broken filter.
    if cat and cat in categories:
        scripts = [s for s in scripts if s.category == cat]
    return _page(request, "home.html", nav="logs",
                 logs=list_logs(),
                 scripts=scripts,
                 categories=categories, cat=cat if cat in categories else "",
                 upload_max_mb=MAX_LOG_UPLOAD_MB,
                 uploads_enabled=MAX_LOG_UPLOAD_MB > 0)


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    if redirect := _need_login(request):
        return redirect
    try:
        result = await logs_api.upload_log(file=file)
    except HTTPException as exc:
        return RedirectResponse("/?" + urlencode({"err": str(exc.detail)}), status_code=303)
    return RedirectResponse("/?" + urlencode({"msg": f"Uploaded {result['saved']}"}), status_code=303)


@router.get("/logs/preview")
def log_preview(request: Request, path: str, lines: int = LOG_PREVIEW_LINES, tail: bool = False):
    if redirect := _need_login(request):
        return redirect
    lines = min(max(1, lines), 2000)
    try:
        result = logs_api.preview_log(path, lines, tail)
    except HTTPException as exc:
        return _render_error(request, exc, back="/")
    return _page(request, "preview.html", nav="logs", preview=result, tail=not tail,
                 req_path=request.query_params.get("path", path), preview_lines=lines)


# --- run configuration + submission ---------------------------------------

@router.get("/run")
def run_page(
    request: Request,
    log: str = "",
    logs: list[str] | None = Query(None),
    scripts: list[str] | None = Query(None),
    pick: list[str] | None = Query(None),
    mode: str = "parallel",
    label: str = "",
    cat: str = "",
    pipeline: str = "",
    msg: str = "",
):
    if redirect := _need_login(request):
        return redirect

    # Parameter values ride along as `p::<script_id>::<param>` query params
    # (exactly what the form would submit), so an Update, a step reorder,
    # a category chip and a shared link all keep the configuration intact.
    url_params: dict[str, dict[str, str]] = {}
    for key in request.query_params:
        if key.startswith("p::") and key.count("::") == 2:
            _, sid, pname = key.split("::", 2)
            url_params.setdefault(sid, {})[pname] = request.query_params[key]

    available = {l.name: l for l in list_logs()}
    selected = _selected_logs(log, logs)
    # A selected entry is usable when the listing knows it, or when it is
    # an absolute path to an existing file — the "use a file by path"
    # escape hatch on the home page.
    def _usable(name: str) -> bool:
        return name in available or (Path(name).is_absolute() and Path(name).is_file())
    missing = [n for n in selected if not _usable(n)]
    selected = [n for n in selected if _usable(n)]

    all_scripts = script_access.visible_scripts(auth.current_user(request))
    categories = sorted({s.category for s in all_scripts if s.category})
    cat = cat if cat in categories else ""
    # The table is filtered for browsing; chosen steps stay chosen via their
    # hidden inputs even when their category is filtered out of view.
    table_scripts = [s for s in all_scripts if s.category == cat] if cat else all_scripts
    by_id = {s.id: s for s in all_scripts}
    # `scripts` carries the chosen steps in order (URL params and the form's
    # hidden inputs). `pick` is the script table's checkboxes: an Update
    # keeps the carried order for still-checked steps, appends newly checked
    # ones in table order, and drops unchecked ones — so a chain order that
    # was reordered survives an Update instead of snapping back to the
    # table's alphabetical order.
    carried = [s for s in (scripts or []) if s in by_id]
    if pick is not None:
        checked = [p for p in pick if p in by_id]
        chosen_ids = [s for s in carried if s in checked]
        chosen_ids += [p for p in checked if p not in carried]
    else:
        chosen_ids = carried
    if mode not in ("parallel", "chain"):
        mode = "parallel"

    # A saved pipeline (loaded via ?pipeline=) replaces the current steps
    # with its own, in order, and forces chain mode; its parameters ride
    # along as values so a loaded pipeline is immediately runnable. Values
    # from the URL still win over the stored ones — a shared link can
    # tweak one knob of a loaded pipeline. Success flashes (msg) come from
    # base.html; this page adds its own error flash for load problems.
    pipelines = pipeline_store.list_pipelines()
    error_msg = ""
    param_values = url_params
    if pipeline:
        loaded = next((p for p in pipelines if p.name == pipeline), None)
        if loaded is None:
            error_msg = f"No pipeline named '{pipeline}'"
        else:
            chosen_ids = [st.script_id for st in loaded.steps if st.script_id in by_id]
            mode = "chain"
            merged = {
                st.script_id: {pname: _stringify_param(pval)
                               for pname, pval in (st.params or {}).items()}
                for st in loaded.steps if st.script_id in by_id
            }
            for sid, vals in url_params.items():
                merged.setdefault(sid, {}).update(vals)
            param_values = merged
            dropped = len(loaded.steps) - len(chosen_ids)
            if dropped:
                error_msg = (f"Pipeline '{pipeline}': {dropped} step(s) skipped — "
                             "script(s) no longer exist")

    # Values belong to the chosen steps — a script dropped from the run
    # does not keep its values in rebuilt links.
    chosen = set(chosen_ids)
    param_values = {sid: vals for sid, vals in param_values.items() if sid in chosen}

    # Category chips and chain reorder links rebuild this exact configuration
    # with one thing swapped, so neither ever drops the selection — or the
    # parameter values.
    def _run_href(cat_value: str, order: list[str] | None = None) -> str:
        ids = chosen_ids if order is None else order
        pairs = [("logs", n) for n in selected]
        pairs += [("scripts", s) for s in ids]
        pairs.append(("mode", mode))
        if label:
            pairs.append(("label", label))
        if cat_value:
            pairs.append(("cat", cat_value))
        for sid in ids:
            for pname, pval in param_values.get(sid, {}).items():
                pairs.append((f"p::{sid}::{pname}", pval))
        return "/run?" + urlencode(pairs, safe=":")

    # A chain runs the steps in the order shown; each step's legend gets
    # ↑/↓ links that swap it with a neighbor. Parallel order is irrelevant,
    # so it gets nothing (mirrors the React UI's chain-only step chips).
    step_moves: list[dict] = []
    if mode == "chain":
        for i in range(len(chosen_ids)):
            up = down = None
            if i > 0:
                swapped = list(chosen_ids)
                swapped[i - 1], swapped[i] = swapped[i], swapped[i - 1]
                up = _run_href(cat, swapped)
            if i < len(chosen_ids) - 1:
                swapped = list(chosen_ids)
                swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
                down = _run_href(cat, swapped)
            step_moves.append({"up": up, "down": down})

    return _page(
        request, "run.html", nav="logs",
        selected_logs=selected,
        missing_logs=missing,
        all_scripts=table_scripts,
        table_ids={s.id for s in table_scripts},
        categories=categories, cat=cat,
        all_href=_run_href(""),
        cat_links=[{"name": c, "href": _run_href(c)} for c in categories],
        chosen_ids=set(chosen_ids),
        chosen_scripts=[by_id[i] for i in chosen_ids],
        step_moves=step_moves,
        mode=mode, label=label,
        param_values=param_values,
        pipeline_names=[p.name for p in pipelines],
        msg=msg, error_msg=error_msg,
    )


def _stringify_param(value) -> str:
    """A parameter value as the one string a form field would carry.

    Booleans become true/false, lists their comma-joined form — the same
    shapes _parse_form_params reads back.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _parse_form_params(form, script) -> dict:
    """Collect `p::<script_id>::<param>` fields for one script, typed.

    Mirrors the React ParamForm: booleans are true/false checkboxes (a
    hidden 'false' precedes the checkbox so unchecked is explicit), multi
    values are comma-separated text, everything else is passed as a string
    for the manifest validator to coerce. Empty always means "use default".
    """
    params: dict = {}
    for spec in script.parameters:
        raw = form.get(f"p::{script.id}::{spec.name}")
        if raw is None or raw == "":
            continue
        if spec.type == "boolean":
            params[spec.name] = str(raw).lower() in ("true", "on", "1")
        elif spec.type == "multi":
            values = [v.strip() for v in str(raw).split(",") if v.strip()]
            if values:
                params[spec.name] = values
        else:
            params[spec.name] = str(raw)
    return params


@router.post("/submit")
async def submit(request: Request):
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    log_names = form.getlist("logs")
    script_ids = form.getlist("scripts")
    mode = form.get("mode", "parallel")
    label = (form.get("label") or "").strip()
    cat = (form.get("cat") or "").strip()

    run_url = "/run?" + urlencode(
        [("logs", n) for n in log_names] + [("scripts", s) for s in script_ids]
        + [("mode", mode)] + ([("label", label)] if label else [])
        + ([("cat", cat)] if cat else []))

    if not script_ids:
        return _render_error(request, HTTPException(422, "No script selected"), back=run_url)

    by_id = script_access.visible_by_id(auth.current_user(request))
    unknown = [s for s in script_ids if s not in by_id]
    if unknown:
        return _render_error(request, HTTPException(404, f"Unknown script: {', '.join(unknown)}"),
                             back=run_url)
    try:
        created: list[Job] = []
        # Fan-out: one submission per selected log, same scripts and
        # params. No log selected means a log-free run: one submission
        # with no log at all — scripts that read {log} are rejected by
        # create_jobs, named, with the script that needs one.
        for log_name in log_names or [""]:
            specs = [
                JobSpecIn(script_id=sid, params=_parse_form_params(form, by_id[sid]))
                for sid in script_ids
            ]
            req = JobCreateRequest(log_path=log_name, mode=mode,
                                   label=label or None, jobs=specs)
            created.extend((await jobs_api.create_jobs(req, request))["jobs"])
    except HTTPException as exc:
        return _render_error(request, exc, back=run_url)

    if len(created) == 1:
        return RedirectResponse(f"/jobs/{created[0].id}", status_code=303)
    noun = "chain" if mode == "chain" else "jobs"
    scope = f" on {len(log_names)} log(s)" if log_names else " without a log"
    return RedirectResponse("/jobs?" + urlencode(
        {"msg": f"Submitted {len(created)} {noun}{scope}"}), status_code=303)


@router.post("/pipelines/save")
async def save_pipeline_page(request: Request):
    """The runbar's 💾 button (formaction) — saves the configured steps.

    Reuses the POST /submit form's fields (logs, ordered scripts, mode,
    label, every p:: parameter) so saving never needs a second form. On
    success it redirects back to the same fully-configured /run URL —
    which doubles as the shareable link for the pipeline's configuration.
    """
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    name = (form.get("pipeline_name") or "").strip()
    log_names = form.getlist("logs")
    script_ids = [s for s in form.getlist("scripts") if s]
    mode = form.get("mode", "chain")
    label = (form.get("label") or "").strip()
    cat = (form.get("cat") or "").strip()

    by_id = script_access.visible_by_id(auth.current_user(request))
    pairs = [("logs", n) for n in log_names]
    pairs += [("scripts", s) for s in script_ids]
    pairs.append(("mode", mode))
    if label:
        pairs.append(("label", label))
    if cat:
        pairs.append(("cat", cat))
    for sid in script_ids:
        script = by_id.get(sid)
        if not script:
            continue
        for spec in script.parameters:
            raw = form.get(f"p::{sid}::{spec.name}")
            if raw not in (None, ""):
                pairs.append((f"p::{sid}::{spec.name}", str(raw)))
    run_url = "/run?" + urlencode(pairs, safe=":")

    if not name:
        return _render_error(request, HTTPException(422, "Pipeline name is empty"),
                             back=run_url)
    if not script_ids:
        return _render_error(request, HTTPException(422, "A pipeline needs at least one step"),
                             back=run_url)
    unknown = [s for s in script_ids if s not in by_id]
    if unknown:
        return _render_error(request, HTTPException(404, f"Unknown script: {', '.join(unknown)}"),
                             back=run_url)
    try:
        req = PipelineSaveRequest(name=name, steps=[
            PipelineStep(script_id=sid, params=_parse_form_params(form, by_id[sid]))
            for sid in script_ids])
        pipelines_api.save_pipeline(req, request)
    except HTTPException as exc:
        return _render_error(request, exc, back=run_url)
    return RedirectResponse(
        run_url + "&" + urlencode({"msg": f"Pipeline '{name}' saved"}), status_code=303)


@router.post("/pipelines/delete")
async def delete_pipeline_page(request: Request):
    """The Load card's 🗑 button — deletes a saved pipeline, zero JS.

    Shares the Load form (a formaction + formmethod override on the
    button), so the selected name and the hidden logs come along; success
    redirects back to /run with a flash.
    """
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    name = (form.get("pipeline") or "").strip()
    log_names = form.getlist("logs")
    cat = (form.get("cat") or "").strip()

    back_pairs = [("logs", n) for n in log_names]
    if cat:
        back_pairs.append(("cat", cat))
    back = "/run?" + urlencode(back_pairs, safe=":") if back_pairs else "/run"
    if not name:
        return _render_error(request, HTTPException(422, "No pipeline selected"), back=back)
    try:
        pipelines_api.delete_pipeline(name)
    except HTTPException as exc:
        return _render_error(request, exc, back=back)
    return RedirectResponse(
        back + "&" + urlencode({"msg": f"Pipeline '{name}' deleted"}), status_code=303)


# --- schedules ------------------------------------------------------------

@router.get("/schedules")
def schedules_page(
    request: Request, cron: str = "", name: str = "", pipeline: str = "",
    script: str = "", log: str = "", logpath: str = "", label: str = "",
    enabled: str = "",
):
    """The cron view: every schedule with its clock, plus the create form.

    ?cron= prefills the expression (the preset chips below the form);
    ?schedule=<name> loads one for editing — saving under the same name
    replaces it, a new name forks it, exactly like pipelines.

    The form's "Configure parameters" button submits it to this page via
    GET (formaction/formmethod overrides), so everything typed so far
    rides along as query params and the page re-renders — now with the
    chosen script's parameter fieldset. Same zero-JS trick as the run
    page; the save POST reuses those exact fields.
    """
    if redirect := _need_login(request):
        return redirect
    schedules = schedules_api.list_schedules()["schedules"]  # enriched: next_due, cron_desc, target
    edit_name = request.query_params.get("schedule", "")
    edit = next((s for s in schedules if s["name"] == edit_name), None) if edit_name else None
    logs_list = list_logs()
    listed = {l.name for l in logs_list}

    # Parameter values typed in the fieldset ride as `p::<script>::<param>`
    # query params — exactly what the form submits (booleans arrive as the
    # hidden false + checkbox true pair; the last one wins).
    param_values: dict[str, dict[str, str]] = {}
    for key in request.query_params:
        if key.startswith("p::") and key.count("::") == 2:
            _, sid, pname = key.split("::", 2)
            param_values.setdefault(sid, {})[pname] = request.query_params[key]

    if edit:
        # Edit prefill: a pipeline schedule selects its pipeline; an inline
        # one selects its first script and its saved parameters. A log the
        # listing doesn't know (a path outside LOGS_DIR) goes back into
        # the by-path box it was typed into.
        saved_log = edit["log"] or ""
        form = {
            "name": edit["name"], "cron": edit["cron"],
            "pipeline": edit["pipeline"] or "",
            "script": "" if edit["pipeline"] else
                      (edit["steps"][0]["script_id"] if edit["steps"] else ""),
            "log": saved_log if saved_log in listed else "",
            "logpath": "" if saved_log in listed else saved_log,
            "label": edit["label"] or "",
            "enabled": edit["enabled"],
        }
        sid = form["script"]
        if sid:
            saved = edit["steps"][0].get("params") or {}
            param_values = {sid: {k: _stringify_param(v) for k, v in saved.items()}}
    else:
        # Wizard state: "" everywhere means a fresh form. `enabled` arrives
        # as "" (fresh load), "true" or "false" (hidden+checkbox pair).
        form = {
            "name": name, "cron": cron, "pipeline": pipeline, "script": script,
            "log": log, "logpath": logpath, "label": label,
            "enabled": enabled != "false",
        }

    by_id = script_access.visible_by_id(auth.current_user(request))
    param_script = by_id.get(form["script"]) if form["script"] else None
    schedule_scripts = sorted(
        (s for s in by_id.values() if not s.error),
        key=lambda s: (s.name or s.id).lower())
    return _page(request, "schedules.html", nav="schedules",
                 schedules=schedules,
                 pipeline_names=[p.name for p in pipeline_store.list_pipelines()],
                 logs=logs_list, edit=edit,
                 schedule_scripts=schedule_scripts,
                 form=form, param_script=param_script,
                 param_values=param_values.get(form["script"], {}) if param_script else {})


@router.post("/schedules/save")
async def save_schedule_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    pipeline = (form.get("pipeline") or "").strip()
    script = (form.get("script") or "").strip()
    # A schedule runs either a saved pipeline or one script of its own —
    # the API XOR-checks both empty / both set, same as every client.
    steps: list[PipelineStep] = []
    if script:
        by_id = script_access.visible_by_id(auth.current_user(request))
        if script not in by_id:
            return _render_error(request, HTTPException(404, f"Unknown script: {script}"),
                                  back="/schedules")
        steps = [PipelineStep(script_id=script,
                               params=_parse_form_params(form, by_id[script]))]
    # The by-path box overrides the log select when filled — the same
    # affordance as the home page's "use a file by path".
    log = (form.get("logpath") or "").strip() or (form.get("log") or "").strip()
    req = ScheduleSaveRequest(
        name=(form.get("name") or "").strip(),
        cron=(form.get("cron") or "").strip(),
        pipeline=pipeline or None,
        steps=steps,
        log=log,
        label=(form.get("label") or "").strip() or None,
        enabled=form.get("enabled") not in (None, "", "off", "false"),
    )
    try:
        schedules_api.save_schedule(req, request)
    except HTTPException as exc:
        return _render_error(request, exc, back="/schedules")
    return RedirectResponse(
        "/schedules?" + urlencode({"msg": f"Schedule '{req.name}' saved"}), status_code=303)


@router.post("/schedules/toggle")
async def toggle_schedule_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    name = ((await request.form()).get("name") or "").strip()
    try:
        out = schedules_api.toggle_schedule(name)
        state = "resumed" if out["schedule"]["enabled"] else "paused"
    except HTTPException as exc:
        return _render_error(request, exc, back="/schedules")
    return RedirectResponse(
        "/schedules?" + urlencode({"msg": f"Schedule '{name}' {state}"}), status_code=303)


@router.post("/schedules/run")
async def run_schedule_page(request: Request):
    """The table's ▶ button — same fire path as the ticker, so overlap
    with the previous run is skipped and recorded here too."""
    if redirect := _need_login(request):
        return redirect
    name = ((await request.form()).get("name") or "").strip()
    try:
        out = await schedules_api.run_schedule(name)
    except HTTPException as exc:
        return _render_error(request, exc, back="/schedules")
    result = out["result"]
    flash = {"err" if result.startswith("error") else "msg": f"{name}: {result}"}
    return RedirectResponse("/schedules?" + urlencode(flash), status_code=303)


@router.post("/schedules/delete")
async def delete_schedule_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    name = ((await request.form()).get("name") or "").strip()
    try:
        schedules_api.delete_schedule(name)
    except HTTPException as exc:
        return _render_error(request, exc, back="/schedules")
    return RedirectResponse(
        "/schedules?" + urlencode({"msg": f"Schedule '{name}' deleted"}), status_code=303)


# --- jobs list ------------------------------------------------------------

@router.get("/jobs")
def jobs_page(
    request: Request,
    q: str = "",
    status: str = "",
    user: str = "",
    offset: int = 0,
    sort: str = "created",
    dir: str = "desc",
):
    if redirect := _need_login(request):
        return redirect
    viewer = auth.current_user(request)
    is_admin = viewer is None or viewer.role == "admin"

    # Filter exactly like the API does (label/script/log/id/chain, comma
    # status; the ?user= narrowing is admin oversight).
    filtered = jobs_api.list_jobs(status=status or None, q=q or None,
                                  user=user.strip() or None,
                                  limit=5000, offset=0, request=request)["jobs"]

    key = _SORT_KEYS.get(sort, _SORT_KEYS["created"])
    reverse = dir != "asc"
    filtered = sorted(filtered, key=key, reverse=reverse)

    total = len(filtered)
    offset = max(0, offset)
    page = filtered[offset:offset + PAGE_SIZE]

    def qs(**overrides):
        params = {"q": q, "status": status, "user": user, "sort": sort, "dir": dir}
        params.update(overrides)
        return "/jobs?" + urlencode({k: v for k, v in params.items() if v})

    counts = {f["value"]: 0 for f in STATUS_FILTERS}
    for job in filtered:
        for f in STATUS_FILTERS:
            if not f["value"] or job.status in f["value"].split(","):
                counts[f["value"]] += 1

    return _page(
        request, "jobs.html", nav="jobs",
        jobs=page, total=total, q=q, status=status,
        offset=offset, page_size=PAGE_SIZE,
        sort=sort, dir=dir, qs=qs,
        is_admin=is_admin, user_filter=user.strip(),
        status_filters=[
            {**f, "count": counts[f["value"]],
             "on": status == f["value"] or (not status and not f["value"])}
            for f in STATUS_FILTERS
        ],
        has_active=any(j.status in ACTIVE for j in filtered),
        has_finished=any(j.status not in ACTIVE for j in filtered),
        duration=_duration,
    )


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str):
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    try:
        await jobs_api.cancel_job(job_id, request)
    except HTTPException as exc:
        return _render_error(request, exc, back="/jobs")
    return RedirectResponse(_safe_next(form.get("back") or f"/jobs/{job_id}"), status_code=303)


@router.post("/jobs/{job_id}/delete")
async def job_delete(request: Request, job_id: str):
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    try:
        await jobs_api.delete_job(job_id, request)
    except HTTPException as exc:
        return _render_error(request, exc, back="/jobs")
    return RedirectResponse(_safe_next(form.get("back") or "/jobs"), status_code=303)


@router.post("/jobs/{job_id}/rerun")
async def job_rerun(request: Request, job_id: str):
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    from_step_raw = (form.get("from_step") or "").strip()
    try:
        from_step = int(from_step_raw) if from_step_raw else None
        result = await jobs_api.rerun_job(job_id, request, from_step)
    except (ValueError, HTTPException) as exc:
        detail = str(getattr(exc, "detail", exc))
        return _render_error(request, HTTPException(422, detail), back=f"/jobs/{job_id}")
    return RedirectResponse(f"/jobs/{result['job'].id}", status_code=303)


@router.post("/jobs/clear-finished")
async def jobs_clear_finished(request: Request):
    if redirect := _need_login(request):
        return redirect
    viewer = auth.current_user(request)
    removed = 0
    for job in jobs_api.store.list():
        if job.status in ACTIVE:
            continue  # only finished jobs are cleared
        if not jobs_api._may_modify(job, viewer):
            # a regular user clears their own finished runs; the shared
            # system history is the admin's to sweep
            continue
        jobs_api.store.delete(job.id)
        removed += 1
    return RedirectResponse("/jobs?" + urlencode({"msg": f"Deleted {removed} finished job(s)"}), status_code=303)


@router.post("/jobs/prune")
async def jobs_prune(request: Request):
    if redirect := _need_login(request):
        return redirect
    form = await request.form()
    try:
        days = int((form.get("days") or "").strip())
        result = jobs_api.prune_jobs(days, request)
    except (ValueError, HTTPException) as exc:
        detail = str(getattr(exc, "detail", exc))
        return RedirectResponse("/jobs?" + urlencode({"err": detail}), status_code=303)
    msg = f"Pruned {result['removed']} job(s) older than {days}d"
    if result.get("cache_removed"):
        msg += f"; freed {result['cache_removed']} decompressed cache file(s)"
    return RedirectResponse("/jobs?" + urlencode({"msg": msg}), status_code=303)


# --- job detail ------------------------------------------------------------

@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str, tab: str = "result"):
    if redirect := _need_login(request):
        return redirect
    try:
        job = jobs_api._get_visible_job_or_404(
            job_id, auth.current_user(request))
    except HTTPException as exc:
        return _render_error(request, exc, back="/jobs")
    if tab not in ("result", "stderr", "input", "files", "details"):
        tab = "result"

    detail = jobs_api._job_detail(job)
    store = jobs_api.store

    # Finished jobs show the full output interpreted like the React
    # OutputView (JSON array -> sortable table); running ones tail live.
    output = None
    if tab == "result" and job.status not in ACTIVE:
        output = _output_view(
            store.read(job_id, "stdout"),
            request.query_params.get("view", "table"),
            request.query_params.get("sortcol", ""),
            -1 if request.query_params.get("sortdir") == "-1" else 1,
        )

    input_text = None
    input_missing = False
    if tab == "input":
        path = jobs_api.executor.input_path(job)
        text = store.read_path(path) if path else None
        if text is None:
            input_missing = True
        else:
            input_text = text
    # A log-free job never had an input at all — a different message than
    # a lost one (deleted log, pruned data, waiting step).
    input_none = not (job.input_path or job.log_path)
    files = []
    if tab == "files":
        for name in job.output_files:
            f = store.outputs_dir(job_id) / name
            files.append({"name": name, "size": f.stat().st_size if f.is_file() else 0})

    return _page(
        request, "job.html", nav="jobs",
        job=job, detail=detail, tab=tab,
        active=job.status in ACTIVE,
        duration=_duration(job),
        output=output,
        input_text=input_text, input_missing=input_missing, input_none=input_none,
        files=files,
    )


@router.get("/jobs/{job_id}/result.csv")
def job_result_csv(request: Request, job_id: str, sortcol: str = "", sortdir: int = 1):
    """The job's JSON-array stdout as CSV, sorted as shown in the table."""
    if redirect := _need_login(request):
        return redirect
    try:
        jobs_api._get_visible_job_or_404(job_id, auth.current_user(request))
    except HTTPException as exc:
        return _render_error(request, exc, back=f"/jobs/{job_id}")
    output = _output_view(jobs_api.store.read(job_id, "stdout"), "table",
                          sortcol, -1 if sortdir == -1 else 1)
    if output["kind"] != "table":
        return _render_error(request, HTTPException(
            422, "Job output is not a JSON table — only array output can be exported"),
            back=f"/jobs/{job_id}")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(output["columns"])
    for row in output["rows"]:
        writer.writerow([_csv_value(row.get(c)) for c in output["columns"]])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="output.csv"'},
    )


@router.get("/jobs/{job_id}/compare")
def job_compare(request: Request, job_id: str, other: str = ""):
    if redirect := _need_login(request):
        return redirect
    try:
        job = jobs_api._get_visible_job_or_404(
            job_id, auth.current_user(request))
    except HTTPException as exc:
        return _render_error(request, exc, back="/jobs")

    # comparing is reading: only jobs this viewer may see can be the other
    # side — someone else's private history must not leak through a diff
    candidates = [j for j in jobs_api.store.list()
                  if j.status == "completed" and j.id != job_id
                  and jobs_api._may_see(j, auth.current_user(request))]
    if not other:
        # default: latest run of the same script, else the latest other job
        sibling = next((j for j in candidates if j.script_id == job.script_id), None)
        other = (sibling or candidates[0]).id if candidates else ""

    other_job = next((j for j in candidates if j.id == other), None)
    diff = None
    if other_job is not None:
        diff = _line_diff(jobs_api.store.read(job_id, "stdout"),
                          jobs_api.store.read(other, "stdout"))

    return _page(request, "compare.html", nav="jobs",
                 job=job, other=other_job, other_id=other,
                 candidates=candidates, diff=diff,
                 max_diff_lines=MAX_DIFF_LINES)


# --- scripts: list, editor, test-run ---------------------------------------

def _editing_disabled() -> HTTPException:
    return HTTPException(403, "Script editing is disabled on this server "
                              "(LOGSMITH_ALLOW_SCRIPT_EDITING=0)")


@router.get("/scripts")
def scripts_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    return _page(request, "scripts.html", nav="scripts",
                 scripts=script_access.visible_scripts(auth.current_user(request)),
                 allow_editing=ALLOW_SCRIPT_EDITING)


def _script_form_values(request: Request, sid: str) -> dict:
    if sid:
        src = scripts_api.get_script_source(sid, request)
        return {
            "id": sid,
            "original_id": sid,
            "extension": src["extension"],
            "name": src["manifest"]["name"],
            "description": src["manifest"]["description"],
            "category": src["manifest"]["category"] or "",
            "command": src["manifest"]["command"],
            "timeout": src["manifest"]["timeout"],
            "output_filename": src["manifest"]["output_filename"] or "",
            "parameters_text": json.dumps(src["manifest"]["parameters"], indent=2),
            "code": src["code"],
        }
    return {
        "id": "", "original_id": "", "extension": "py",
        "name": "", "description": "", "category": "", "command": "",
        "timeout": 600, "output_filename": "",
        "parameters_text": "[]", "code": STARTER_CODE,
    }


def _script_form_context(request: Request, values: dict, form_error: str = "",
                         status_code: int = 200):
    return _page(request, "script_form.html", nav="scripts", status_code=status_code,
                 values=values, logs=list_logs(), extensions=EXTENSIONS,
                 form_error=form_error)


def _form_values_from(form) -> dict:
    """Rebuild editor values from a submitted form (keeps work on errors)."""
    return {
        "id": form.get("id") or "",
        "original_id": form.get("original_id") or "",
        "extension": form.get("extension") or "py",
        "name": form.get("name") or "",
        "description": form.get("description") or "",
        "category": form.get("category") or "",
        "command": form.get("command") or "",
        "timeout": form.get("timeout") or "",
        "output_filename": form.get("output_filename") or "",
        "parameters_text": form.get("parameters") or "[]",
        "code": form.get("code") or "",
    }


@router.get("/scripts/new")
def script_new(request: Request):
    if redirect := _need_login(request):
        return redirect
    if not ALLOW_SCRIPT_EDITING:
        return RedirectResponse("/scripts?" + urlencode(
            {"err": "Script editing is disabled on this server"}), status_code=303)
    return _script_form_context(request, _script_form_values(request, ""))


@router.get("/scripts/{script_id}/edit")
def script_edit(request: Request, script_id: str):
    if redirect := _need_login(request):
        return redirect
    if not ALLOW_SCRIPT_EDITING:
        return RedirectResponse("/scripts?" + urlencode(
            {"err": "Script editing is disabled on this server"}), status_code=303)
    try:
        values = _script_form_values(request, script_id)
    except HTTPException as exc:
        return _render_error(request, exc, back="/scripts")
    return _script_form_context(request, values)


def _script_upsert_request(form) -> ScriptUpsertRequest:
    """Build a ScriptUpsertRequest from the editor form (or raise 422)."""
    sid = (form.get("id") or "").strip()
    ext = form.get("extension") or "py"
    try:
        timeout = int(form.get("timeout") or 0)
    except ValueError:
        raise HTTPException(422, detail="timeout must be an integer number of seconds")
    params_raw = (form.get("parameters") or "").strip() or "[]"
    try:
        parameters = json.loads(params_raw)
        if not isinstance(parameters, list):
            raise ValueError("not a JSON array")
    except ValueError as exc:
        raise HTTPException(422, detail=f"Parameters must be a JSON array of parameter objects: {exc}")
    try:
        manifest = ScriptManifest(
            name=(form.get("name") or "").strip(),
            description=(form.get("description") or "").strip(),
            category=(form.get("category") or "").strip(),
            command=(form.get("command") or "").strip(),
            timeout=timeout,
            output_filename=(form.get("output_filename") or "").strip() or None,
            parameters=parameters,
        )
        return ScriptUpsertRequest(id=sid, extension=ext,  # type: ignore[arg-type]
                                    code=form.get("code") or "", manifest=manifest)
    except ValidationError as exc:
        raise HTTPException(422, detail=f"Invalid script: {exc.errors()[0]['msg']}")


def _save_script(request: Request, form) -> str:
    """Create or update from the form; returns the saved script id.

    An existing id that differs from `original_id` saves as a new script
    (the editor's duplicate flow) instead of failing the id match.
    """
    original = (form.get("original_id") or "").strip()
    req = _script_upsert_request(form)
    if original and req.id == original:
        scripts_api.update_script(original, req, request)
    else:
        scripts_api.create_script(req, request)
    return req.id


@router.post("/scripts/save")
async def script_save(request: Request):
    if redirect := _need_login(request):
        return redirect
    if not ALLOW_SCRIPT_EDITING:
        return _render_error(request, _editing_disabled(), back="/scripts")
    form = await request.form()
    try:
        sid = _save_script(request, form)
    except HTTPException as exc:
        return _script_form_context(request, _form_values_from(form),
                                     form_error=str(exc.detail),
                                     status_code=exc.status_code)
    return RedirectResponse(
        f"/scripts/{sid}/edit?" + urlencode({"msg": "Script saved"}), status_code=303)


@router.post("/scripts/test-run")
async def script_test_run(request: Request):
    """Save the form's current state, then run it on a chosen log.

    Mirrors the React editor's test-run: manifest defaults are used (no
    parameter overrides) and the browser lands on the job page.
    """
    if redirect := _need_login(request):
        return redirect
    if not ALLOW_SCRIPT_EDITING:
        return _render_error(request, _editing_disabled(), back="/scripts")
    form = await request.form()
    log = (form.get("log") or "").strip()
    if not log:
        return _script_form_context(request, _form_values_from(form),
                                    form_error="Pick a log for the test run",
                                    status_code=422)
    try:
        sid = _save_script(request, form)
        result = await jobs_api.create_jobs(JobCreateRequest(
            log_path=log, mode="parallel",
            jobs=[JobSpecIn(script_id=sid, params={})]), request)
    except HTTPException as exc:
        return _script_form_context(request, _form_values_from(form),
                                    form_error=str(exc.detail),
                                    status_code=exc.status_code)
    return RedirectResponse(f"/jobs/{result['jobs'][0].id}", status_code=303)


@router.post("/scripts/{script_id}/delete")
def script_delete(request: Request, script_id: str):
    if redirect := _need_login(request):
        return redirect
    if not ALLOW_SCRIPT_EDITING:
        return _render_error(request, _editing_disabled(), back="/scripts")
    try:
        scripts_api.delete_script(script_id, request)
    except HTTPException as exc:
        return _render_error(request, exc, back="/scripts")
    return RedirectResponse("/scripts?" + urlencode({"msg": f"Deleted {script_id}"}), status_code=303)


@router.post("/scripts/import")
async def script_import(request: Request, file: UploadFile = File(...),
                        name: str = Form(""), overwrite: str = Form("")):
    """The Import form on /scripts: one tar.gz, applied in-process.

    Same contract as the API (all-or-nothing, 409 on existing ids unless
    the replace box is checked); the flash carries the result either way.
    """
    if redirect := _need_login(request):
        return redirect
    if not ALLOW_SCRIPT_EDITING:
        return _render_error(request, _editing_disabled(), back="/scripts")
    try:
        result = await scripts_api.import_scripts(
            file=file, name=(name or "").strip(),
            overwrite=overwrite in ("1", "true", "on", "yes"),
            request=request,
        )
    except HTTPException as exc:
        return RedirectResponse("/scripts?" + urlencode({"err": str(exc.detail)}),
                                status_code=303)
    ids = ", ".join(result["imported"])
    return RedirectResponse("/scripts?" + urlencode(
        {"msg": f"Imported {len(result['imported'])} script(s): {ids}"}), status_code=303)


# --- scripts: share / unshare (multi-user) -------------------------------------

@router.post("/scripts/{script_id}/share")
def script_share(request: Request, script_id: str):
    if redirect := _need_login(request):
        return redirect
    try:
        scripts_api.share_script(script_id, request)
    except HTTPException as exc:
        return _render_error(request, exc, back="/scripts")
    return RedirectResponse("/scripts?" + urlencode(
        {"msg": f"Shared {script_id} — it is now in the shared library"}), status_code=303)


@router.post("/scripts/{script_id}/unshare")
def script_unshare(request: Request, script_id: str):
    if redirect := _need_login(request):
        return redirect
    try:
        scripts_api.unshare_script(script_id, request)
    except HTTPException as exc:
        return _render_error(request, exc, back="/scripts")
    return RedirectResponse("/scripts?" + urlencode(
        {"msg": f"Unshared {script_id} — it moved to your private scripts"}),
        status_code=303)


# --- users (admin) -----------------------------------------------------------

_ADMIN_BACK = "/admin/users"


@router.get("/admin/users")
def users_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    try:
        users_api.require_admin(request)
    except HTTPException as exc:
        return _render_error(request, exc, back="/")
    return _page(request, "users.html", nav="users", users=users_api.list_users(request)["users"])


@router.post("/admin/users")
def user_create(request: Request, username: str = Form(""), password: str = Form(""),
                role: str = Form("user")):
    if redirect := _need_login(request):
        return redirect
    # built up front so create_user can mint the bootstrap session onto it
    # (creating the first account flips multi-user mode on — the operator
    # stays signed in as the new admin instead of hitting /login)
    response = RedirectResponse(_ADMIN_BACK + "?" + urlencode(
        {"msg": f"Created {username.strip()} ({role})"}), status_code=303)
    try:
        users_api.create_user(
            users_api.CreateBody(username=username, password=password, role=role),
            request, response)
    except HTTPException as exc:
        return RedirectResponse(_ADMIN_BACK + "?" + urlencode({"err": str(exc.detail)}),
                                status_code=303)
    return response


@router.post("/admin/users/{username}/password")
def user_password(request: Request, username: str, password: str = Form("")):
    if redirect := _need_login(request):
        return redirect
    try:
        users_api.reset_password(username, users_api.PasswordBody(password=password),
                                 request)
    except HTTPException as exc:
        return RedirectResponse(_ADMIN_BACK + "?" + urlencode({"err": str(exc.detail)}),
                                status_code=303)
    return RedirectResponse(_ADMIN_BACK + "?" + urlencode(
        {"msg": f"Password reset for {username} (their sessions were revoked)"}),
        status_code=303)


@router.post("/admin/users/{username}/disable")
def user_disable(request: Request, username: str):
    if redirect := _need_login(request):
        return redirect
    try:
        users_api.disable_user(username, request)
    except HTTPException as exc:
        return RedirectResponse(_ADMIN_BACK + "?" + urlencode({"err": str(exc.detail)}),
                                status_code=303)
    return RedirectResponse(_ADMIN_BACK + "?" + urlencode({"msg": f"Disabled {username}"}),
                            status_code=303)


@router.post("/admin/users/{username}/enable")
def user_enable(request: Request, username: str):
    if redirect := _need_login(request):
        return redirect
    try:
        users_api.enable_user(username, request)
    except HTTPException as exc:
        return RedirectResponse(_ADMIN_BACK + "?" + urlencode({"err": str(exc.detail)}),
                                status_code=303)
    return RedirectResponse(_ADMIN_BACK + "?" + urlencode({"msg": f"Enabled {username}"}),
                            status_code=303)


@router.post("/admin/users/{username}/role")
def user_role(request: Request, username: str, role: str = Form("")):
    if redirect := _need_login(request):
        return redirect
    try:
        users_api.change_role(username, users_api.RoleBody(role=role), request)
    except HTTPException as exc:
        return RedirectResponse(_ADMIN_BACK + "?" + urlencode({"err": str(exc.detail)}),
                                status_code=303)
    return RedirectResponse(_ADMIN_BACK + "?" + urlencode(
        {"msg": f"{username} is now {role}"}), status_code=303)


# --- terminal ---------------------------------------------------------------

VENDOR_DIR = Path(__file__).parent / "vendor"

# The python UI's interactive shell rides on vendored xterm.js builds (see
# backend/app/vendor/README.md) — served only while the terminal is on so
# the flag-off contract ("nothing terminal answers") covers the assets too.
_TERMINAL_ASSETS = {
    "xterm.js": "text/javascript",
    "addon-fit.js": "text/javascript",
    "terminal.js": "text/javascript",
    "xterm.css": "text/css",
}


@router.get("/terminal/assets/{name}")
def terminal_asset(name: str):
    if not TERMINAL_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    media_type = _TERMINAL_ASSETS.get(name)
    path = VENDOR_DIR / name if media_type else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type=media_type)


@router.get("/terminal")
def terminal_page(request: Request):
    """The command box (zero JS): one command, its output, a back button.

    The interactive shell lives in the React tab — a pty cannot be driven
    from plain HTML forms. While LOGSMITH_TERMINAL is off this is a 404,
    matching what the API does.
    """
    if redirect := _need_login(request):
        return redirect
    if not TERMINAL_ENABLED:
        raise HTTPException(status_code=404, detail="Terminal is disabled")
    return _page(request, "terminal.html", nav="terminal")


@router.post("/terminal")
async def terminal_run(request: Request):
    if redirect := _need_login(request):
        return redirect
    if not TERMINAL_ENABLED:
        raise HTTPException(status_code=404, detail="Terminal is disabled")
    form = await request.form()
    command = (form.get("command") or "").strip()
    if not command:
        return _page(request, "terminal.html", nav="terminal",
                     command="", err="Type a command first.")
    result = await terminal_service.run_command(command, timeout=30.0)
    return _page(request, "terminal.html", nav="terminal",
                 command=command, result=result)


# --- catch-all: a real 404 page -------------------------------------------

@router.get("/{full_path:path}")
def page_not_found(request: Request, full_path: str):
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    return _page(request, "error.html", status_code=404,
                 status=404, detail=f"Page not found: /{full_path}", back="/")
