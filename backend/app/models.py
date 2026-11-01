"""Pydantic models shared across the API and services."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ParamType = Literal["string", "integer", "float", "boolean", "enum", "multi"]
JobStatus = Literal["queued", "waiting", "running", "completed", "failed", "cancelled", "skipped"]


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
    style: Literal["flat", "directory"] = "flat"
    output_filename: str | None = None
    error: str | None = None  # set when the manifest failed to load


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


class JobCreateRequest(BaseModel):
    log_path: str
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
