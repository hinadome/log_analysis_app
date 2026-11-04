"""Pydantic models shared across the API and services."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

ParamType = Literal["string", "integer", "float", "boolean", "enum", "multi"]
JobStatus = Literal["queued", "waiting", "running", "completed", "failed", "cancelled", "skipped"]
UserRole = Literal["admin", "user"]


class User(BaseModel):
    """An account. The password never lives here — only its scrypt hash."""

    id: str
    username: str
    password_hash: str  # "scrypt$N$r$p$salt_hex$hash_hex"
    role: UserRole = "user"
    created_at: str = ""
    disabled: bool = False


class ParamSpec(BaseModel):
    """One CLI parameter of a script, as declared in its manifest."""

    name: str
    flag: str
    label: str = ""
    type: ParamType = "string"
    choices: list[str] = Field(default_factory=list)
    default: Any = None
    required: bool = False
    help: str = ""


class ScriptManifest(BaseModel):
    """Manifest data (parsed from a sidecar JSON or manifest.json)."""

    name: str = ""
    description: str = ""
    category: str = ""  # optional grouping label, e.g. "web" — pickers offer it as a filter
    command: str = ""
    timeout: int = 600
    parameters: list[ParamSpec] = Field(default_factory=list)
    entry: str | None = None  # directory-style scripts only
    output_filename: str | None = None


class ScriptInfo(BaseModel):
    """A script as exposed by the API."""

    id: str
    name: str
    description: str = ""
    category: str = ""
    command: str
    timeout: int
    parameters: list[ParamSpec] = Field(default_factory=list)
    script_path: str
    # Username of the account this script belongs to (its private space,
    # scripts/users/<name>/) — None means the shared library at the top
    # level of scripts/. Ownership is locational: no index file.
    owner: str | None = None
    style: Literal["flat", "directory"] = "flat"
    output_filename: str | None = None
    error: str | None = None  # set when the manifest failed to load

    @computed_field
    @property
    def needs_log(self) -> bool:
        """Whether the command reads the input log ({log} placeholder).

        Derived, not declared: a script without {log} in its command can
        run without a selected log. A script that reads $JOB_LOG_PATH
        directly instead of the placeholder is on its own — the env var
        is only set when a log was selected.
        """
        return "{log}" in self.command


class ScriptUpsertRequest(BaseModel):
    """Create or replace a script through the UI editor (flat style)."""

    id: str
    extension: Literal["py", "sh", "bash", "zsh", "js", "rb", "pl"] = "py"
    code: str
    manifest: ScriptManifest


class LogFile(BaseModel):
    name: str
    path: str
    size: int
    modified: str


class JobSpecIn(BaseModel):
    script_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class PipelineStep(BaseModel):
    """One step of a saved pipeline: a script plus its parameters."""
    script_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class PipelineSaveRequest(BaseModel):
    name: str
    steps: list[PipelineStep]


class Pipeline(BaseModel):
    """A saved, reusable chain — steps run in order on any log."""
    name: str
    steps: list[PipelineStep] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class ScheduleSaveRequest(BaseModel):
    name: str
    cron: str
    pipeline: str | None = None
    steps: list[PipelineStep] = Field(default_factory=list)  # inline: script(s), no pipeline
    log: str = ""
    label: str | None = None
    enabled: bool = True


class Schedule(BaseModel):
    """A saved pipeline — or inline steps — set to run on one log at
    cron-defined times.

    Exactly one of `pipeline` (a saved pipeline's name, resolved at fire
    time so editing the pipeline updates the schedule) or `steps` (a
    validated snapshot owned by the schedule itself) is set; a single
    script scheduled directly is `steps` of length one.

    The bookkeeping fields are written by the scheduler, never by the
    save API: `last_run_at` is the local-time minute of the last fire
    (naive ISO, matching the cron matcher's clock — it doubles as the
    double-fire guard for the 30-second ticker), and `last_result` /
    `last_job_ids` describe what that fire did. A schedule disabled at
    fire time simply does not run; a fire missed while the server was
    down is not caught up.
    """
    name: str
    cron: str
    pipeline: str | None = None      # saved pipeline name; fires as a chain
    steps: list[PipelineStep] = Field(default_factory=list)
    log: str = ""                    # log path as saved; empty = log-free
    label: str | None = None   # job label; defaults to the schedule name
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    last_run_at: str | None = None
    last_result: str | None = None    # "ok: …" | "skipped: …" | "error: …"
    last_job_ids: list[str] = Field(default_factory=list)


class JobCreateRequest(BaseModel):
    log_path: str = ""  # optional: scripts without {log} run without one
    mode: Literal["parallel", "chain"] = "parallel"
    label: str | None = None  # optional user tag for the whole submission
    jobs: list[JobSpecIn]


class Job(BaseModel):
    id: str
    script_id: str
    script_name: str
    log_path: str
    log_name: str
    label: str | None = None  # user tag for the submission this job belongs to
    params: dict[str, Any] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    status: JobStatus = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    stdout_size: int = 0
    stderr_size: int = 0
    output_files: list[str] = Field(default_factory=list)

    # Chain (pipeline) support: jobs in one submission linked by chain_id,
    # executed sequentially — each step's {log} is the previous step's output.
    chain_id: str | None = None
    chain_index: int = 0
    chain_size: int = 0

    # Cache path of the decompressed copy this job reads, for a compressed
    # input (the selected log or a previous step's compressed artifact).
    # Memoized at submit time so the spawned command, the materialized file
    # and the Input tab always agree even if the source changes underneath.
    decompressed_path: str | None = None
    input_source: str | None = None  # "log" | "prev_output" | "prev_stdout"
    input_path: str | None = None     # actual file the {log} placeholder resolved to
    output_file: str | None = None    # {output} artifact target, when declared
