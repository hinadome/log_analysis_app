#!/usr/bin/env python3
"""scriptengine — the ScriptEngine command-line client.

A single-file client for the ScriptEngine API, dependency-free (stdlib only:
argparse, urllib, json). Everything the web UIs can do is here: browse
logs, run scripts (live stdout streaming), manage scripts, pipelines,
schedules, job history and — for admins — user accounts.

Quick start:

    scriptengine login                    # once per server (session is saved)
    scriptengine logs list
    scriptengine run grep_search:pattern=ERROR --log app.log
    scriptengine jobs list

Authentication: `login` stores a session cookie in ~/.scriptengine.json and
every later command sends it. The legacy shared password (single-user
servers with SCRIPTENGINE_TOKEN) can be used directly instead:
`--token`/SCRIPTENGINE_TOKEN sends it as a Bearer header.

Steps (used by run, pipelines save, schedules save) are written as

    script_id:param=value,param=value

Values are read as JSON when they parse (2 → int, true → bool,
["a","b"] → list) and left as raw strings otherwise; a value containing
a comma must be quoted ("a,b"); repeating a parameter makes it a list.
Exit codes: 0 success, 1 a job failed or was cancelled, 2 any other
error (bad usage, HTTP failure, not logged in).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

COOKIE_NAME = "scriptengine_session"
DEFAULT_URL = "http://127.0.0.1:8000"
POLL_INTERVAL = 0.5           # seconds between job polls while following
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


# --- configuration ---------------------------------------------------------

def config_path() -> Path:
    """Where login state lives: $SCRIPTENGINE_CONFIG or ~/.scriptengine.json."""
    return Path(os.environ.get("SCRIPTENGINE_CONFIG", Path.home() / ".scriptengine.json"))


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # it holds a session id
    except OSError:
        pass


def server_url(args, cfg: dict) -> str:
    """--url > SCRIPTENGINE_URL > the config file > the localhost default."""
    url = getattr(args, "url", None) or os.environ.get("SCRIPTENGINE_URL") \
        or cfg.get("url") or DEFAULT_URL
    return url.rstrip("/")


# --- HTTP ------------------------------------------------------------------

class ApiError(Exception):
    """An HTTP failure or connection problem, message ready to print."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Client:
    """A thin JSON-over-HTTP wrapper with auth and cookie capture."""

    def __init__(self, url: str, token: str | None = None,
                 session: str | None = None):
        self.url = url
        self.token = token
        self.session = session
        self.cookies: list[str] = []   # raw Set-Cookie values of the last response

    # -- plumbing --

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.session:
            headers["Cookie"] = f"{COOKIE_NAME}={self.session}"
        return headers

    def request(self, method: str, path: str, *, body: bytes | None = None,
                headers: dict | None = None, params: dict | None = None,
                timeout: float = 60.0):
        """One request; returns the response object (or raises ApiError)."""
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        all_headers = self._headers()
        if headers:
            all_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=all_headers,
                                     method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except OSError:
                pass
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise ApiError(exc.code, str(detail) or exc.reason) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ApiError(0, f"cannot reach {self.url} ({reason}) — "
                              "is the server running?") from None

    def _json_or_none(self, resp):
        raw = resp.read()
        self.cookies = resp.headers.get_all("Set-Cookie") or []
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    # -- convenience verbs --

    def get_json(self, path: str, params: dict | None = None):
        with self.request("GET", path, params=params) as resp:
            return self._json_or_none(resp)

    def send_json(self, method: str, path: str, payload) -> dict:
        body = json.dumps(payload).encode("utf-8")
        with self.request(method, path, body=body,
                          headers={"Content-Type": "application/json"}) as resp:
            return self._json_or_none(resp)

    def get_text(self, path: str) -> str:
        with self.request("GET", path,
                          headers={"Accept": "text/plain"}) as resp:
            return resp.read().decode("utf-8", "replace")

    def get_bytes(self, path: str) -> bytes:
        with self.request("GET", path) as resp:
            return resp.read()

    def post_query(self, path: str, params: dict | None = None):
        """POST with no body, options as query params (cancel, rerun, prune)."""
        with self.request("POST", path, params=params) as resp:
            return self._json_or_none(resp)

    def post_multipart(self, path: str, fields: dict[str, str],
                       files: list[tuple[str, str, bytes, str]]):
        """fields + files as multipart/form-data (log/script uploads)."""
        boundary = "----scriptengine-" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n".encode("utf-8"))
        for name, filename, content, ctype in files:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
                .encode("utf-8") + content + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        with self.request("POST", path, body=b"".join(chunks),
                          headers={"Content-Type":
                                   f"multipart/form-data; boundary={boundary}"}) as resp:
            return self._json_or_none(resp)

    def session_cookie(self) -> str | None:
        """The scriptengine_session value of the last response, if any."""
        for header in self.cookies:
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME and value:
                    return value.strip('"')
        return None


def make_client(args) -> tuple[Client, dict]:
    """A client with whatever auth the flags/env/config provide."""
    cfg = load_config()
    token = getattr(args, "token", None) or os.environ.get("SCRIPTENGINE_TOKEN") or None
    session = None if token else cfg.get("session")
    return Client(server_url(args, cfg), token=token, session=session), cfg


# --- output helpers --------------------------------------------------------

def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n} B"


def print_table(headers: list[str], rows: list[list]) -> None:
    rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(f"{{:<{w}}}" for w in widths)
    print(line.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line.format(*row))


def print_json(payload) -> None:
    print(json.dumps(payload, indent=2))


def kv(label: str, value, indent: int = 0) -> None:
    print(f"{' ' * indent}{label:<16}{value}")


# --- step specs: script_id:param=value,... ---------------------------------

def _split_pairs(spec: str) -> list[str]:
    """Split on commas that are outside quotes and brackets — so JSON
    values like ["a","b"] or "x,y" survive intact."""
    parts, buf, depth, quote, escaped = [], [], 0, False, False
    for ch in spec:
        if quote:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _decode_value(raw: str):
    """JSON when it parses (2, true, null, ["a","b"]), raw string otherwise."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def parse_step(spec: str) -> tuple[str, dict]:
    """'script_id:param=value,...' → (script_id, params dict).

    Repeating a parameter collects its values into a list (the 'multi'
    parameter type); quoting a value keeps commas inside it.
    """
    script_id, _, rest = spec.partition(":")
    script_id = script_id.strip()
    if not script_id:
        raise ValueError(f"empty script id in step '{spec}'")
    params: dict = {}
    for pair in _split_pairs(rest):
        if not pair:
            continue
        name, sep, raw = pair.partition("=")
        if not sep or not name:
            raise ValueError(f"bad parameter '{pair}' in step '{spec}' "
                             "(expected name=value)")
        value = _decode_value(raw)
        if name in params:  # repeated → list
            existing = params[name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                params[name] = [existing, value]
        else:
            params[name] = value
    return script_id, params


def parse_steps(specs: list[str]) -> list[dict]:
    steps = []
    for spec in specs:
        script_id, params = parse_step(spec)
        steps.append({"script_id": script_id, "params": params})
    return steps


# --- following jobs (polling) ----------------------------------------------

def _emit(line: str, prefix: str | None) -> None:
    sys.stdout.write((f"{prefix}{line}\n") if prefix else f"{line}\n")
    sys.stdout.flush()


def follow_jobs(client: Client, jobs: list[dict], interval: float) -> int:
    """Poll the jobs, streaming stdout as it grows; return an exit code.

    Single job: stdout is printed bare (pipe-friendly). Multiple: each
    line is prefixed with the script name. A failed job's stderr tail is
    printed to stderr.
    """
    ids = [j["id"] for j in jobs]
    names = {j["id"]: j.get("script_name") or j.get("script_id", "?")
             for j in jobs}
    prefix_for = (lambda jid: f"[{names[jid]}] ") if len(ids) > 1 else (lambda jid: None)
    offset = {jid: 0 for jid in ids}
    buffer: dict[str, str] = {jid: "" for jid in ids}
    finished: dict[str, dict] = {}
    pending = list(ids)

    while pending:
        for jid in list(pending):
            detail = client.get_json(f"/api/jobs/{jid}")
            try:
                text = client.get_text(f"/api/jobs/{jid}/stdout")
            except ApiError:
                text = ""
            buffer[jid] += text[offset[jid]:]
            offset[jid] = len(text)
            lines = buffer[jid].split("\n")
            buffer[jid] = lines.pop()          # hold the partial line back
            for line in lines:
                _emit(line, prefix_for(jid))
            if detail["status"] in TERMINAL_STATUSES:
                pending.remove(jid)
                finished[jid] = detail
                if buffer[jid]:
                    _emit(buffer[jid], prefix_for(jid))
                    buffer[jid] = ""
        if pending:
            time.sleep(interval)

    for jid, detail in finished.items():
        if detail["status"] != "completed":
            tail = detail.get("stderr_tail") or ""
            for line in (tail.splitlines() or [""]):
                if line.strip():
                    sys.stderr.write(f"scriptengine: {line}\n")
            sys.stderr.write(
                f"scriptengine: job {jid} ({names[jid]}) {detail['status']}"
                + (f", exit {detail['exit_code']}"
                   if detail.get("exit_code") is not None else "") + "\n")
    if len(finished) > 1:
        ok = sum(1 for d in finished.values() if d["status"] == "completed")
        sys.stderr.write(f"scriptengine: {len(finished)} jobs: {ok} completed, "
                         f"{len(finished) - ok} failed/cancelled\n")
    return 0 if all(d["status"] == "completed" for d in finished.values()) else 1


def jobs_from_response(payload: dict) -> list[dict]:
    return payload.get("jobs") or []


# --- command implementations -----------------------------------------------

def cmd_status(args) -> int:
    client, cfg = make_client(args)
    print(f"server:  {client.url}")
    print(f"config:  {config_path()}")
    if client.token:
        print("auth:    SCRIPTENGINE_TOKEN (Bearer, legacy shared password)")
    elif client.session:
        print(f"auth:    session cookie ({cfg.get('username') or 'unknown user'})")
    else:
        print("auth:    none (anonymous)")
    try:
        status = client.get_json("/api/auth/status") or {}
    except ApiError as exc:
        print(f"reach:   FAILED — {exc}")
        return 2
    if status.get("multi_user"):
        user = status.get("user")
        who = (f"logged in as {user['username']} ({user['role']})"
               if user else "not logged in (accounts exist)")
        print(f"mode:    multi-user — {who}")
    else:
        print(f"mode:    single-user — authentication "
              f"{'required' if status.get('auth_required') else 'disabled'}")
    if not client.token and not client.session:
        return 0
    try:  # /api/health needs auth; it doubles as a session check
        health = client.get_json("/api/health") or {}
        print(f"health:  {health.get('status')} "
              f"(max {health.get('max_concurrent_jobs')} concurrent jobs, "
              f"retention {health.get('job_retention_days')}d, "
              f"scheduler {'on' if health.get('scheduler') else 'off'}, "
              f"terminal {'on' if health.get('terminal') else 'off'})")
    except ApiError as exc:
        print(f"health:  FAILED — {exc}")
        if exc.status == 401:
            print("         the saved session is stale — run `scriptengine login`")
    return 0


def cmd_login(args) -> int:
    cfg = load_config()
    url = server_url(args, cfg)
    username = args.username or ""
    if not username:
        username = input("Username (blank on single-password servers): ").strip()
    password = args.password or getpass.getpass("Password: ")
    client = Client(url)   # fresh: no prior auth on the login itself
    client.send_json("POST", "/api/auth/login",
                     {"username": username, "password": password})
    session = client.session_cookie()
    if session is None:
        print("This server has no authentication — nothing to log into.")
        cfg["url"] = url
        save_config(cfg)
        return 0
    # verify the session and pick up the identity it belongs to
    who = Client(url, session=session).get_json("/api/auth/status") or {}
    cfg.update({"url": url, "session": session,
                "username": (who.get("user") or {}).get("username", username)})
    save_config(cfg)
    user = who.get("user")
    if who.get("multi_user") and user:
        print(f"Logged in as {user['username']} ({user['role']}) — {url}")
    else:
        print(f"Logged in — {url} (single-user server)")
    return 0


def cmd_logout(args) -> int:
    client, cfg = make_client(args)
    try:
        client.send_json("POST", "/api/auth/logout", {})
    except ApiError:
        pass  # a dead server still deserves a local logout
    cfg.pop("session", None)
    cfg.pop("username", None)
    save_config(cfg)
    print("Logged out.")
    return 0


def cmd_logs_list(args) -> int:
    client, _ = make_client(args)
    logs = (client.get_json("/api/logs") or {}).get("logs", [])
    if args.json:
        print_json(logs)
        return 0
    print_table(["NAME", "SIZE", "MODIFIED", "PATH"],
               [[l["name"], human_size(l["size"]), l["modified"], l["path"]]
                for l in logs])
    return 0


def cmd_logs_preview(args) -> int:
    client, _ = make_client(args)
    body = client.get_json("/api/logs/preview",
                           {"path": args.log, "lines": args.lines,
                            "tail": "true" if args.tail else "false"}) or {}
    if args.json:
        print_json(body)
        return 0
    for line in body.get("lines", []):
        print(line)
    if body.get("truncated"):
        which = "last" if args.tail else "first"
        print(f"[showing the {which} {len(body.get('lines', []))} lines — "
              f"file is larger]", file=sys.stderr)
    return 0


def cmd_logs_upload(args) -> int:
    client, _ = make_client(args)
    path = Path(args.file)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ApiError(0, f"cannot read {path}: {exc}")
    body = client.post_multipart(
        "/api/logs/upload", {},
        [("file", path.name, content, "application/octet-stream")]) or {}
    if args.json:
        print_json(body)
        return 0
    log = body.get("log") or {}
    print(f"uploaded {log.get('name', path.name)} "
          f"({human_size(body.get('size', len(content)))})")
    return 0


def cmd_scripts_list(args) -> int:
    client, _ = make_client(args)
    scripts = (client.get_json("/api/scripts") or {}).get("scripts", [])
    if args.json:
        print_json(scripts)
        return 0
    rows = []
    for s in scripts:
        name = s["name"] + ("  [broken]" if s.get("error") else "")
        rows.append([s["id"], name, s.get("category") or "",
                     s.get("owner") or "shared",
                     str(len(s.get("parameters", [])))])
    print_table(["ID", "NAME", "CATEGORY", "OWNER", "PARAMS"], rows)
    return 0


def cmd_scripts_show(args) -> int:
    client, _ = make_client(args)
    body = client.get_json(f"/api/scripts/{args.script_id}") or {}
    if args.json:
        print_json(body)
        return 0
    sys.stdout.write(body.get("code", ""))
    if not body.get("code", "").endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _extension_command(extension: str) -> str:
    """The default command template for an extension (mirrors the scanner)."""
    interpreter = {".py": "python3", ".sh": "bash", ".bash": "bash",
                    ".zsh": "zsh", ".js": "node", ".rb": "ruby",
                    ".pl": "perl"}.get(f".{extension}", "")
    return (f"{interpreter} {{script}} {{log}} {{args}}" if interpreter
            else "{script} {log} {args}")


def _default_manifest(script_id: str, extension: str) -> dict:
    return {"name": script_id, "command": _extension_command(extension),
            "parameters": []}


def _load_manifest(path: str | None, script_id: str, extension: str) -> dict:
    if not path:
        return _default_manifest(script_id, extension)
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApiError(0, f"cannot read manifest {path}: {exc}")
    if not isinstance(manifest, dict):
        raise ApiError(0, f"manifest {path} is not a JSON object")
    return manifest


def _apply_command_options(args, manifest: dict, extension: str) -> dict:
    """--command overrides the template; --log-free strips {log} from it.

    Together they cover the editor's command field, including its "no
    target file" case: a command without {log} makes the script log-free,
    runnable with no log selected.
    """
    command = (getattr(args, "command_template", None) or "").strip()
    if command:
        manifest["command"] = command
    if getattr(args, "log_free", False):
        base = (manifest.get("command") or "").strip() \
            or _extension_command(extension)
        tokens = []
        for token in base.split():
            token = token.replace("{log}", "")
            if token and not token.endswith("="):   # {log} glued to a flag?
                tokens.append(token)                # the flag went with it
        manifest["command"] = " ".join(tokens) or "{script} {args}"
    return manifest


def _read_code(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ApiError(0, f"cannot read code file {path}: {exc}")


def cmd_scripts_create(args) -> int:
    client, _ = make_client(args)
    extension = args.ext or Path(args.file).suffix.lstrip(".") or "py"
    manifest = _apply_command_options(
        args, _load_manifest(args.manifest, args.script_id, extension),
        extension)
    payload = {"id": args.script_id, "extension": extension,
               "code": _read_code(args.file),
               "manifest": manifest}
    body = client.send_json("POST", "/api/scripts", payload) or {}
    if args.json:
        print_json(body)
        return 0
    script = body.get("script") or {}
    print(f"created {script.get('id', args.script_id)} "
          f"({'shared' if script.get('owner') is None else 'private to ' + script['owner']})")
    return 0


def cmd_scripts_update(args) -> int:
    client, _ = make_client(args)
    current = client.get_json(f"/api/scripts/{args.script_id}") or {}
    extension = args.ext or current.get("extension", "py")
    if args.manifest:
        manifest = _load_manifest(args.manifest, args.script_id, extension)
    else:  # code-only update: keep the manifest as-is
        manifest = current.get("manifest")
        if manifest is None:
            manifest = _load_manifest(None, args.script_id, extension)
    manifest = _apply_command_options(args, manifest, extension)
    payload = {"id": args.script_id, "extension": extension,
               "code": _read_code(args.file), "manifest": manifest}
    body = client.send_json("PUT", f"/api/scripts/{args.script_id}", payload) or {}
    if args.json:
        print_json(body)
        return 0
    print(f"updated {args.script_id}")
    return 0


def cmd_scripts_delete(args) -> int:
    client, _ = make_client(args)
    client.request("DELETE", f"/api/scripts/{args.script_id}").read()
    print(f"deleted {args.script_id}")
    return 0


def cmd_scripts_share(args) -> int:
    client, _ = make_client(args)
    body = client.send_json("POST", f"/api/scripts/{args.script_id}/share", {}) or {}
    if args.json:
        print_json(body)
        return 0
    print(f"shared {args.script_id}")
    return 0


def cmd_scripts_unshare(args) -> int:
    client, _ = make_client(args)
    body = client.send_json("POST", f"/api/scripts/{args.script_id}/unshare", {}) or {}
    if args.json:
        print_json(body)
        return 0
    print(f"unshared {args.script_id} (now private to you)")
    return 0


def cmd_scripts_export(args) -> int:
    client, _ = make_client(args)
    path = (f"/api/scripts/{args.script_id}/export" if args.script_id
            else "/api/scripts/export")
    data = client.get_bytes(path)
    out = Path(args.out or (f"{args.script_id}.tar.gz" if args.script_id
                            else "scriptengine-scripts.tar.gz"))
    out.write_bytes(data)
    print(f"wrote {out} ({human_size(len(data))})")
    return 0


def cmd_scripts_import(args) -> int:
    client, _ = make_client(args)
    archive = Path(args.archive)
    try:
        content = archive.read_bytes()
    except OSError as exc:
        raise ApiError(0, f"cannot read {archive}: {exc}")
    params = {"overwrite": "1" if args.overwrite else "0"}
    if args.name:
        params["name"] = args.name
    body = client.post_multipart(
        "/api/scripts/import", params,
        [("file", archive.name, content, "application/gzip")]) or {}
    if args.json:
        print_json(body)
        return 0
    print(f"imported: {', '.join(body.get('imported', []))}")
    return 0


def submit(client: Client, steps: list[dict], log: str, mode: str,
           label: str | None) -> dict:
    payload = {"jobs": steps, "mode": mode}
    if log:
        payload["log_path"] = log
    if label:
        payload["label"] = label
    return client.send_json("POST", "/api/jobs", payload) or {}


def print_submitted(jobs: list[dict]) -> None:
    print_table(["ID", "SCRIPT", "STATUS", "LOG"],
                [[j["id"], j.get("script_id"), j.get("status"),
                  j.get("log_name") or ""] for j in jobs])


def cmd_run(args) -> int:
    client, _ = make_client(args)
    jobs = jobs_from_response(
        submit(client, parse_steps(args.steps), args.log, args.mode, args.label))
    if args.no_wait:
        if args.json:
            print_json(jobs)
        else:
            print_submitted(jobs)
        return 0
    return follow_jobs(client, jobs, args.interval)


def cmd_jobs_list(args) -> int:
    client, _ = make_client(args)
    params = {"limit": args.limit, "offset": args.offset}
    if args.status:
        params["status"] = ",".join(args.status)
    if args.q:
        params["q"] = args.q
    if args.user:
        params["user"] = args.user
    body = client.get_json("/api/jobs", params) or {}
    if args.json:
        print_json(body)
        return 0
    jobs = body.get("jobs", [])
    print_table(["ID", "STATUS", "EXIT", "SCRIPT", "LOG", "LABEL", "CREATED"],
                [[j["id"], j["status"],
                  "" if j.get("exit_code") is None else j["exit_code"],
                  j.get("script_name") or j.get("script_id"),
                  j.get("log_name") or "", j.get("label") or "",
                  j.get("created_at") or ""] for j in jobs])
    total = body.get("total", len(jobs))
    if total > len(jobs):
        print(f"[{len(jobs)} of {total} jobs — narrow with --status/--q, "
              "page with --offset]", file=sys.stderr)
    return 0


def cmd_jobs_show(args) -> int:
    client, _ = make_client(args)
    job = client.get_json(f"/api/jobs/{args.job_id}") or {}
    if args.json:
        print_json(job)
        return 0
    kv("job", job["id"])
    kv("script", f"{job.get('script_name')} ({job.get('script_id')})")
    kv("status", job.get("status")
       + (f" (exit {job['exit_code']})" if job.get("exit_code") is not None else ""))
    kv("log", job.get("log_name") or "(none)")
    if job.get("label"):
        kv("label", job["label"])
    if job.get("params"):
        kv("params", json.dumps(job["params"]))
    if job.get("command"):
        kv("command", " ".join(job["command"]), indent=0)
    kv("created", job.get("created_at"))
    kv("output", f"stdout {human_size(job.get('stdout_size', 0))}, "
                 f"stderr {human_size(job.get('stderr_size', 0))}")
    if job.get("output_files"):
        kv("files", ", ".join(job["output_files"]))
    if job.get("chain_id"):
        kv("chain", f"step {job.get('chain_index', 0) + 1} of "
                    f"{job.get('chain_size')} ({job['chain_id']})")
        for step in job.get("chain_steps") or []:
            mark = {"completed": "✓", "failed": "✗", "running": "…"}.get(
                step.get("status"), " ")
            print(f"         {mark} {step['index'] + 1}. {step['script_name']}"
                  f" — {step['status']}")
    return 0


def cmd_jobs_output(args) -> int:
    client, _ = make_client(args)
    which = "stderr" if args.stderr else ("input" if args.input else "stdout")
    sys.stdout.write(client.get_text(f"/api/jobs/{args.job_id}/{which}"))
    return 0


def cmd_jobs_files(args) -> int:
    client, _ = make_client(args)
    job = client.get_json(f"/api/jobs/{args.job_id}") or {}
    files = job.get("output_files") or []
    if args.json:
        print_json(files)
        return 0
    for name in files:
        print(name)
    if not files:
        print("(no output files)", file=sys.stderr)
    return 0


def cmd_jobs_get(args) -> int:
    client, _ = make_client(args)
    data = client.get_bytes(f"/api/jobs/{args.job_id}/files/{args.filename}")
    out = Path(args.out or args.filename)
    out.write_bytes(data)
    print(f"wrote {out} ({human_size(len(data))})")
    return 0


def cmd_jobs_cancel(args) -> int:
    client, _ = make_client(args)
    client.post_query(f"/api/jobs/{args.job_id}/cancel")
    print(f"cancelled {args.job_id}")
    return 0


def cmd_jobs_delete(args) -> int:
    client, _ = make_client(args)
    client.request("DELETE", f"/api/jobs/{args.job_id}").read()
    print(f"deleted {args.job_id}")
    return 0


def _chain_jobs(client: Client, first: dict) -> list[dict]:
    """All jobs of the (possible) chain a job belongs to, in step order."""
    detail = client.get_json(f"/api/jobs/{first['id']}") or {}
    steps = detail.get("chain_steps")
    if not steps:
        return [first]
    return [{"id": s["id"], "script_name": s["script_name"]} for s in steps]


def cmd_jobs_rerun(args) -> int:
    client, _ = make_client(args)
    params = {"from_step": args.from_step} if args.from_step else None
    body = client.post_query(f"/api/jobs/{args.job_id}/rerun", params) or {}
    job = body.get("job") or {}
    if args.no_wait:
        if args.json:
            print_json(job)
        else:
            print_submitted([job])
        return 0
    return follow_jobs(client, _chain_jobs(client, job), args.interval)


def cmd_jobs_prune(args) -> int:
    client, _ = make_client(args)
    params = {"days": args.days} if args.days else None
    body = client.post_query("/api/jobs/prune", params) or {}
    if args.json:
        print_json(body)
        return 0
    print(f"removed {body.get('removed', 0)} job(s) older than "
          f"{body.get('retention_days', args.days)} day(s)")
    return 0


def cmd_pipelines_list(args) -> int:
    client, _ = make_client(args)
    pipelines = (client.get_json("/api/pipelines") or {}).get("pipelines", [])
    if args.json:
        print_json(pipelines)
        return 0
    print_table(["NAME", "STEPS", "UPDATED"],
               [[p["name"],
                 " → ".join(s["script_id"] for s in p.get("steps", [])),
                 p.get("updated_at") or ""] for p in pipelines])
    return 0


def cmd_pipelines_save(args) -> int:
    client, _ = make_client(args)
    payload = {"name": args.name, "steps": parse_steps(args.steps)}
    body = client.send_json("POST", "/api/pipelines", payload) or {}
    if args.json:
        print_json(body)
        return 0
    pipeline = body.get("pipeline") or {}
    print(f"saved pipeline '{pipeline.get('name', args.name)}' "
          f"({len(pipeline.get('steps', []))} steps)")
    return 0


def cmd_pipelines_delete(args) -> int:
    client, _ = make_client(args)
    client.request("DELETE", f"/api/pipelines/{urllib.parse.quote(args.name)}").read()
    print(f"deleted pipeline '{args.name}'")
    return 0


def cmd_pipelines_run(args) -> int:
    client, _ = make_client(args)
    pipelines = (client.get_json("/api/pipelines") or {}).get("pipelines", [])
    match = next((p for p in pipelines if p["name"] == args.name), None)
    if match is None:
        raise ApiError(0, f"no pipeline named '{args.name}'")
    jobs = jobs_from_response(
        submit(client, match.get("steps", []), args.log, "chain", args.label))
    if args.no_wait:
        if args.json:
            print_json(jobs)
        else:
            print_submitted(jobs)
        return 0
    return follow_jobs(client, jobs, args.interval)


def cmd_schedules_list(args) -> int:
    client, _ = make_client(args)
    schedules = (client.get_json("/api/schedules") or {}).get("schedules", [])
    if args.json:
        print_json(schedules)
        return 0
    print_table(["NAME", "TARGET", "CRON", "NEXT DUE", "ON"],
                [[s["name"], s.get("target") or "",
                  s.get("cron") or "",
                  s.get("next_due") or "—",
                  "yes" if s.get("enabled") else "no"] for s in schedules])
    return 0


def cmd_schedules_save(args) -> int:
    client, _ = make_client(args)
    scripts = getattr(args, "scripts", None) or []
    if bool(args.pipeline) == bool(scripts):
        raise ApiError(0, "a schedule runs either a saved pipeline "
                          "(--pipeline NAME) or its own script(s) "
                          "(--script STEP, repeatable), not both")
    payload = {"name": args.name, "cron": args.cron, "log": args.log,
               "enabled": not args.disabled}
    if args.pipeline:
        payload["pipeline"] = args.pipeline
    else:
        payload["steps"] = parse_steps(scripts)
    if args.label:
        payload["label"] = args.label
    body = client.send_json("POST", "/api/schedules", payload) or {}
    if args.json:
        print_json(body)
        return 0
    schedule = body.get("schedule") or {}
    print(f"saved schedule '{schedule.get('name', args.name)}' — "
          f"{schedule.get('cron_desc', args.cron)}")
    return 0


def cmd_schedules_toggle(args) -> int:
    client, _ = make_client(args)
    body = client.send_json(
        "POST", f"/api/schedules/{urllib.parse.quote(args.name)}/toggle", {}) or {}
    if args.json:
        print_json(body)
        return 0
    schedule = body.get("schedule") or {}
    state = "enabled" if schedule.get("enabled") else "disabled"
    print(f"schedule '{args.name}' {state}")
    return 0


def cmd_schedules_run(args) -> int:
    client, _ = make_client(args)
    body = client.send_json(
        "POST", f"/api/schedules/{urllib.parse.quote(args.name)}/run", {}) or {}
    if args.json:
        print_json(body)
        return 0
    print(body.get("result", "fired"))
    for jid in body.get("job_ids") or []:
        print(f"job {jid}")
    return 0


def cmd_schedules_delete(args) -> int:
    client, _ = make_client(args)
    client.request(
        "DELETE", f"/api/schedules/{urllib.parse.quote(args.name)}").read()
    print(f"deleted schedule '{args.name}'")
    return 0


def _prompt_password(args) -> str:
    return args.password or getpass.getpass(f"Password for {args.username}: ")


def cmd_users_list(args) -> int:
    client, _ = make_client(args)
    users = (client.get_json("/api/users") or {}).get("users", [])
    if args.json:
        print_json(users)
        return 0
    print_table(["USERNAME", "ROLE", "CREATED", "DISABLED"],
                [[u["username"], u["role"], u.get("created_at") or "",
                  "yes" if u.get("disabled") else ""] for u in users])
    return 0


def cmd_users_create(args) -> int:
    client, cfg = make_client(args)
    password = _prompt_password(args)
    body = client.send_json("POST", "/api/users",
                            {"username": args.username, "password": password,
                             "role": args.role}) or {}
    session = client.session_cookie()
    if session is not None:
        # Set-Cookie here means bootstrap: creating the first account
        # minted a session for whoever created it — the CLI just became
        # that admin. Adopt it; any earlier (legacy) session is dead now.
        cfg.update({"url": client.url, "session": session,
                    "username": args.username})
        save_config(cfg)
        note = " (adopted the bootstrap session — you are now this admin)"
    else:
        note = ""
    if args.json:
        print_json(body)
        return 0
    user = body.get("user") or {}
    print(f"created {user.get('username', args.username)} "
          f"({user.get('role', args.role)}){note}")
    return 0


def cmd_users_password(args) -> int:
    client, _ = make_client(args)
    password = _prompt_password(args)
    client.send_json("POST",
                     f"/api/users/{urllib.parse.quote(args.username)}/password",
                     {"password": password})
    print(f"password set for {args.username} (their sessions were revoked)")
    return 0


def cmd_users_role(args) -> int:
    client, _ = make_client(args)
    client.send_json("POST",
                     f"/api/users/{urllib.parse.quote(args.username)}/role",
                     {"role": args.role})
    print(f"{args.username} is now {args.role}")
    return 0


def cmd_users_disable(args) -> int:
    client, _ = make_client(args)
    client.send_json("POST",
                     f"/api/users/{urllib.parse.quote(args.username)}/disable", {})
    print(f"disabled {args.username} (their sessions were revoked)")
    return 0


def cmd_users_enable(args) -> int:
    client, _ = make_client(args)
    client.send_json("POST",
                     f"/api/users/{urllib.parse.quote(args.username)}/enable", {})
    print(f"enabled {args.username}")
    return 0


# --- parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", help="server base URL "
                      "(default: $SCRIPTENGINE_URL, the config file, or "
                      f"{DEFAULT_URL})")
    common.add_argument("--token", help="legacy shared-password token "
                      "(default: $SCRIPTENGINE_TOKEN)")
    common.add_argument("--json", action="store_true",
                        help="print the raw JSON response")

    parser = argparse.ArgumentParser(
        prog="scriptengine",
        description="The ScriptEngine command-line client.",
        epilog="Steps are 'script_id:param=value,...' — values parse as JSON "
               "when valid (2, true, [\"a\",\"b\"]), raw strings otherwise; "
               "quote values containing commas; repeating a parameter makes "
               "a list. Examples:\n"
               "  scriptengine run grep_search:pattern=ERROR,context=2 --log app.log\n"
               "  scriptengine run --mode chain grep_search:pattern=ERROR "
               "error_counter:level=ERROR --log app.log\n"
               "  scriptengine pipelines save errors grep_search:pattern=ERROR "
               "error_counter:level=ERROR\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    top = parser.add_subparsers(dest="command", required=True,
                                metavar="COMMAND")

    def sub(parent, name: str, help_text: str, func):
        p = parent.add_parser(name, help=help_text, parents=[common])
        p.set_defaults(func=func)
        return p

    def group(name: str, help_text: str):
        """A nested command group (scriptengine logs list, scripts show, ...)."""
        p = top.add_parser(name, help=help_text)
        return p.add_subparsers(dest="subcommand", required=True,
                                metavar="SUBCOMMAND")

    sub(top, "status", "server info and who you are", cmd_status)

    p = sub(top, "login", "authenticate and save the session", cmd_login)
    p.add_argument("username", nargs="?", help="account name (blank for "
                                               "single-password servers)")
    p.add_argument("--password", help="the password (prompted when omitted — "
                                       "prefer the prompt, flags live in "
                                       "shell history)")

    sub(top, "logout", "drop the saved session", cmd_logout)

    g = group("logs", "browse and upload log files")
    sub(g, "list", "list the server's log files", cmd_logs_list)
    p = sub(g, "preview", "print the first/last lines of a log",
            cmd_logs_preview)
    p.add_argument("log", help="log name (or path) as shown by 'logs list'")
    p.add_argument("--lines", type=int, default=50)
    p.add_argument("--tail", action="store_true", help="show the last lines")
    p = sub(g, "upload", "upload a log file to the server", cmd_logs_upload)
    p.add_argument("file")

    g = group("scripts", "manage analysis scripts")
    sub(g, "list", "list scripts you can run", cmd_scripts_list)
    p = sub(g, "show", "print a script's code (editor payload with --json)",
            cmd_scripts_show)
    p.add_argument("script_id")
    p = sub(g, "create", "create a script from a code file", cmd_scripts_create)
    p.add_argument("script_id")
    p.add_argument("--file", required=True, help="the code file")
    p.add_argument("--manifest", help="manifest JSON (default: a minimal "
                                      "one inferred from the extension)")
    p.add_argument("--command", dest="command_template",
                   help="command template, like the editor's field — e.g. "
                        "'python3 {script} {log} {args}' (default: the "
                        "extension's default)")
    p.add_argument("--log-free", action="store_true",
                   help="no target file: strip {log} from the command — "
                        "the script runs with no log selected")
    p.add_argument("--ext", choices=["py", "sh", "bash", "zsh", "js", "rb", "pl"],
                   help="code extension (default: the --file suffix, or py)")
    p = sub(g, "update", "update a script's code and/or manifest",
            cmd_scripts_update)
    p.add_argument("script_id")
    p.add_argument("--file", required=True, help="the new code file")
    p.add_argument("--manifest", help="manifest JSON (default: keep the "
                                      "current one)")
    p.add_argument("--command", dest="command_template",
                   help="command template — replaces the current one")
    p.add_argument("--log-free", action="store_true",
                   help="no target file: strip {log} from the command — "
                        "the script runs with no log selected")
    p.add_argument("--ext", choices=["py", "sh", "bash", "zsh", "js", "rb", "pl"],
                   help="code extension (default: the script's current one)")
    p = sub(g, "delete", "delete a script", cmd_scripts_delete)
    p.add_argument("script_id")
    p = sub(g, "share", "publish your script to the shared library",
            cmd_scripts_share)
    p.add_argument("script_id")
    p = sub(g, "unshare", "take a shared script private (admin)",
            cmd_scripts_unshare)
    p.add_argument("script_id")
    p = sub(g, "export", "download a script (or all) as tar.gz",
             cmd_scripts_export)
    p.add_argument("script_id", nargs="?",
                   help="one script, or omit for the whole library")
    p.add_argument("--out", help="output file (default: <id>.tar.gz)")
    p = sub(g, "import", "import script(s) from an export archive",
            cmd_scripts_import)
    p.add_argument("archive")
    p.add_argument("--name", help="id for a directory-style archive")
    p.add_argument("--overwrite", action="store_true",
                   help="replace existing scripts with the same id")

    p = sub(top, "run", "submit script(s) and stream their output", cmd_run)
    p.add_argument("steps", nargs="+", metavar="STEP",
                   help="script_id[:param=value,...]")
    p.add_argument("--log", default="", help="log name or path")
    p.add_argument("--mode", choices=["parallel", "chain"], default="parallel",
                   help="how multiple scripts run (default: parallel)")
    p.add_argument("--label", help="a tag for this submission")
    p.add_argument("--no-wait", action="store_true",
                   help="submit only, print the job ids")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL,
                   help="seconds between polls while following (default: "
                        f"{POLL_INTERVAL})")

    g = group("jobs", "inspect and manage job history")
    p = sub(g, "list", "list job history", cmd_jobs_list)
    p.add_argument("--status", action="append",
                   help="filter by status (repeatable: --status failed)")
    p.add_argument("--q", help="search ids, scripts, logs, labels")
    p.add_argument("--user", help="narrow to one owner (admin)")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offset", type=int, default=0)
    p = sub(g, "show", "one job's details", cmd_jobs_show)
    p.add_argument("job_id")
    p = sub(g, "output", "print a job's stdout (or --stderr/--input)",
            cmd_jobs_output)
    p.add_argument("job_id")
    p.add_argument("--stderr", action="store_true")
    p.add_argument("--input", action="store_true",
                   help="what the script received as input")
    p = sub(g, "files", "list a job's output files", cmd_jobs_files)
    p.add_argument("job_id")
    p = sub(g, "get", "download a job's output file", cmd_jobs_get)
    p.add_argument("job_id")
    p.add_argument("filename")
    p.add_argument("--out", help="output file (default: the same name)")
    p = sub(g, "cancel", "cancel a queued/running job", cmd_jobs_cancel)
    p.add_argument("job_id")
    p = sub(g, "delete", "delete a job and its data", cmd_jobs_delete)
    p.add_argument("job_id")
    p = sub(g, "rerun", "run a job (or chain) again", cmd_jobs_rerun)
    p.add_argument("job_id")
    p.add_argument("--from-step", type=int,
                   help="chain rerun: start at this step (1-based)")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL)
    p = sub(g, "prune", "delete jobs older than N days", cmd_jobs_prune)
    p.add_argument("--days", type=int,
                   help="age threshold (default: the server's retention)")

    g = group("pipelines", "saved reusable chains")
    sub(g, "list", "list saved pipelines", cmd_pipelines_list)
    p = sub(g, "save", "save (or replace) a pipeline", cmd_pipelines_save)
    p.add_argument("name")
    p.add_argument("steps", nargs="+", metavar="STEP")
    p = sub(g, "delete", "delete a pipeline", cmd_pipelines_delete)
    p.add_argument("name")
    p = sub(g, "run", "run a pipeline as a chain", cmd_pipelines_run)
    p.add_argument("name")
    p.add_argument("--log", default="", help="log name or path")
    p.add_argument("--label")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL)

    g = group("schedules", "cron-scheduled runs")
    sub(g, "list", "list schedules", cmd_schedules_list)
    p = sub(g, "save", "save (or replace) a schedule", cmd_schedules_save)
    p.add_argument("name")
    p.add_argument("--cron", required=True,
                   help="cron expression, e.g. '*/5 * * * *'")
    p.add_argument("--pipeline", help="run this saved pipeline")
    p.add_argument("--script", dest="scripts", action="append", metavar="STEP",
                   help="inline step (script_id[:param=value,...]); "
                        "repeatable — a schedule runs a pipeline or "
                        "script(s), not both")
    p.add_argument("--log", default="", help="log name or path")
    p.add_argument("--label")
    p.add_argument("--disabled", action="store_true",
                   help="save disabled (default: enabled)")
    p = sub(g, "toggle", "enable/disable a schedule", cmd_schedules_toggle)
    p.add_argument("name")
    p = sub(g, "run", "fire a schedule now", cmd_schedules_run)
    p.add_argument("name")
    p = sub(g, "delete", "delete a schedule", cmd_schedules_delete)
    p.add_argument("name")

    g = group("users", "manage accounts (admin)")
    sub(g, "list", "list accounts", cmd_users_list)
    p = sub(g, "create", "create an account (the first one bootstraps "
                         "multi-user mode)", cmd_users_create)
    p.add_argument("username")
    p.add_argument("--password", help="prompted when omitted")
    p.add_argument("--role", choices=["user", "admin"], default="user")
    p = sub(g, "password", "reset a password", cmd_users_password)
    p.add_argument("username")
    p.add_argument("--password", help="prompted when omitted")
    p = sub(g, "role", "change a role", cmd_users_role)
    p.add_argument("username")
    p.add_argument("--role", choices=["user", "admin"], required=True)
    p = sub(g, "disable", "disable an account", cmd_users_disable)
    p.add_argument("username")
    p = sub(g, "enable", "re-enable an account", cmd_users_enable)
    p.add_argument("username")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ApiError as exc:
        hint = ""
        if exc.status == 401 and args.command != "login":
            hint = (" — run `scriptengine login` first (or set SCRIPTENGINE_TOKEN "
                    "on single-password servers)")
        elif exc.status == 403 and "admin" in str(exc).lower():
            hint = " — this needs an admin account"
        print(f"scriptengine: {exc}{hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("scriptengine: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
