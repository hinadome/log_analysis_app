"""The Docker sandbox executor path (SCRIPTENGINE_DOCKER_SANDBOX=1).

Two layers:

- Unit tests build the sandbox argv and assert its shape — mounts, network
  off, user, env, container name — without needing Docker at all.
- End-to-end tests (skipped unless Docker and the image are available)
  actually run jobs in the sandbox. `docker build -f sandbox.Dockerfile
  -t scriptengine-sandbox .` prepares them (run.sh does it on start).

The flag is module-level state read at import, so tests monkeypatch
executor.DOCKER_SANDBOX per test — same pattern test_auth.py and
test_security.py use for their flags.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import app.services.executor as executor_module
import app.services.scanner as scanner_module
import app.routers.scripts as scripts_router
import app.routers.jobs as jobs_router
from app.config import SANDBOX_IMAGE
from app.models import Job
from conftest import SAMPLE_LOG, make_script

from test_api import submit, wait_status


@pytest.fixture()
def sandbox(monkeypatch):
    monkeypatch.setattr(executor_module, "DOCKER_SANDBOX", True)


@pytest.fixture()
def scripts_dir(tmp_path, monkeypatch):
    """Redirect SCRIPTS_DIR so e2e tests never touch the repo's samples."""
    target = tmp_path / "scripts"
    target.mkdir()
    monkeypatch.setattr(scanner_module, "SCRIPTS_DIR", target)
    monkeypatch.setattr(scripts_router, "SCRIPTS_DIR", target)
    return target


# --- unit tests: the docker run wrapper --------------------------------------

def _job(job_id="20261030-120000-deadbeef") -> Job:
    return Job(id=job_id, script_id="error_counter", script_name="Error Counter",
               log_path=str(SAMPLE_LOG), log_name=SAMPLE_LOG.name,
               command=["python3", "/x/error_counter.py", str(SAMPLE_LOG)])


def _script_info():
    for script in scanner_module.discover_scripts():
        if script.id == "error_counter":
            return script
    pytest.fail("error_counter sample script not found")


def test_sandbox_off_by_default():
    """Without the flag nothing changes — direct execution."""
    assert executor_module.DOCKER_SANDBOX is False


def test_sandbox_argv_shape(sandbox):
    """The wrapper mounts exactly what a job needs, nothing more."""
    ex = jobs_router.executor
    job = _job()
    script = _script_info()
    input_path = Path(job.log_path)
    out_dir = ex._store.outputs_dir(job.id)

    argv = ex._sandbox_argv(job, script, input_path)

    assert argv[0:2] == ["docker", "run"]
    i = argv.index
    assert "--rm" in argv
    assert argv[i("--network") + 1] == "none"
    assert argv[i("--name") + 1] == f"scriptengine-job-{job.id}"
    assert argv[i("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert argv[i("--memory") + 1] == executor_module.SANDBOX_MEMORY
    assert argv[i("--workdir") + 1] == str(out_dir)

    mounts = [argv[k + 1] for k, a in enumerate(argv) if a == "-v"]
    # script, input and original log read-only at identical paths
    assert f"{script.script_path}:{script.script_path}:ro" in mounts
    assert f"{input_path}:{input_path}:ro" in mounts
    # the outputs dir is the one writable mount, at the same path too
    writable = [m for m in mounts if not m.endswith(":ro")]
    assert writable == [f"{out_dir}:{out_dir}"]

    # JOB_* env passes explicitly; the container never inherits the host env
    envs = [argv[k + 1] for k, a in enumerate(argv) if a == "-e"]
    assert envs[0] == "HOME=/tmp"
    assert f"JOB_ID={job.id}" in envs
    assert f"JOB_LOG_PATH={input_path}" in envs
    assert f"JOB_ORIGINAL_LOG={job.log_path}" in envs
    assert f"JOB_OUTPUT_DIR={out_dir}" in envs

    # image, then the job's own command unchanged
    assert argv[i(SANDBOX_IMAGE) + 1: i(SANDBOX_IMAGE) + 1 + len(job.command)] == job.command


def test_sandbox_argv_log_free_job(sandbox):
    """A log-free job: no input mount, no JOB_LOG_PATH / JOB_ORIGINAL_LOG —
    only the script and the outputs dir are mounted."""
    ex = jobs_router.executor
    job = Job(id="20261030-120000-cafef00d", script_id="disk_report",
              script_name="Disk Report", log_path="", log_name="",
              command=["python3", "/x/disk_report.py"])
    script = make_script("disk_report", "python3 {script}")
    out_dir = ex._store.outputs_dir(job.id)

    argv = ex._sandbox_argv(job, script, None)

    envs = [argv[k + 1] for k, a in enumerate(argv) if a == "-e"]
    assert not any(e.startswith("JOB_LOG_PATH=") for e in envs)
    assert not any(e.startswith("JOB_ORIGINAL_LOG=") for e in envs)
    assert f"JOB_ID={job.id}" in envs  # the rest of the contract stands
    assert f"JOB_OUTPUT_DIR={out_dir}" in envs

    mounts = [argv[k + 1] for k, a in enumerate(argv) if a == "-v"]
    assert f"{script.script_path}:{script.script_path}:ro" in mounts
    writable = [m for m in mounts if not m.endswith(":ro")]
    assert writable == [f"{out_dir}:{out_dir}"]
    assert len(mounts) == 2  # script + outputs, nothing else


def test_sandbox_argv_mounts_original_log_once(sandbox):
    """Compressed input: the materialized copy AND the original are mounted,
    but an uncompressed log is not mounted twice."""
    ex = jobs_router.executor
    job = _job()
    script = _script_info()
    decompressed = ex._store.job_dir(job.id) / "input.log"

    argv = ex._sandbox_argv(job, script, decompressed)
    mounts = [argv[k + 1] for k, a in enumerate(argv) if a == "-v"]
    assert f"{decompressed}:{decompressed}:ro" in mounts
    assert f"{job.log_path}:{job.log_path}:ro" in mounts

    argv = ex._sandbox_argv(job, script, Path(job.log_path))
    mounts = [argv[k + 1] for k, a in enumerate(argv) if a == "-v"]
    assert sum(m.startswith(f"{job.log_path}:") for m in mounts) == 1


# --- docker availability gate --------------------------------------------------

def _docker_ok() -> bool:
    """Daemon reachable AND the sandbox image built? Used at collection
    time, so every probe is fast or bounded."""
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
    except Exception:
        return False
    try:
        subprocess.run(["docker", "image", "inspect", SANDBOX_IMAGE],
                       capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


docker_required = pytest.mark.skipif(
    not _docker_ok(),
    reason=f"needs the Docker daemon and the {SANDBOX_IMAGE} image "
           f"(docker build -f sandbox.Dockerfile -t scriptengine-sandbox .)",
)


# --- e2e: real sandboxed jobs ----------------------------------------------------

@docker_required
def test_sandboxed_job_completes(sandbox, client):
    """A real script runs to completion inside the container."""
    jobs = submit(client, [{"script_id": "error_counter", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",), timeout=60)
    stdout = client.get(f"/api/jobs/{jobs[0]['id']}/stdout").text
    assert "ERROR" in stdout


@docker_required
def test_sandbox_writes_artifacts_as_host_user(sandbox, client, scripts_dir):
    """Artifacts written inside the container land on the host owned by us."""
    from test_scripts_api import _body
    code = ("import os\n"
            "with open(os.path.join(os.environ['JOB_OUTPUT_DIR'], 'probe.txt'), 'w') as f:\n"
            "    f.write('from-container')\n")
    res = client.post("/api/scripts", json=_body(
        "artifact_writer", extension="py", code=code, command=""))
    assert res.status_code == 200, res.text

    jobs = submit(client, [{"script_id": "artifact_writer", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",), timeout=60)
    probe = jobs_router.store.outputs_dir(jobs[0]["id"]) / "probe.txt"
    assert probe.read_text() == "from-container"
    assert probe.stat().st_uid == os.getuid()  # not root — stays deletable


@docker_required
def test_sandbox_sees_no_network(sandbox, client, scripts_dir):
    """--network none: a script that opens a socket cannot."""
    from test_scripts_api import _body
    code = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('example.com', 443), timeout=5)\n"
            "    print('NETWORK REACHABLE')\n"
            "except OSError:\n"
            "    print('no network - as it should be')\n")
    client.post("/api/scripts", json=_body("net_probe", extension="py", code=code,
                                              command=""))

    jobs = submit(client, [{"script_id": "net_probe", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",), timeout=60)
    stdout = client.get(f"/api/jobs/{jobs[0]['id']}/stdout").text
    assert "NETWORK REACHABLE" not in stdout
    assert "no network" in stdout


@docker_required
def test_sandbox_timeout_kills_container(sandbox, client, scripts_dir):
    """A timed-out sandboxed job fails AND leaves no container behind."""
    from test_scripts_api import _body
    client.post("/api/scripts", json=_body(
        "sleepy", extension="py",
        code="import time\nprint('started', flush=True)\ntime.sleep(120)\n",
        command="", timeout=3))
    jobs = submit(client, [{"script_id": "sleepy", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("failed",), timeout=60)

    detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert "Timed out" in detail["error"]
    import time
    time.sleep(1)  # let --rm finish removing the killed container
    left = subprocess.run(["docker", "ps", "-q", "--filter", "name=scriptengine-job-"],
                          capture_output=True, timeout=15)
    assert left.stdout.strip() == b"", "sandboxed containers must not outlive their jobs"


@docker_required
def test_sandbox_runs_every_interpreter(sandbox, client):
    """Each interpreter the sample scripts use works inside the image."""
    script_ids = ["error_counter", "grep_search", "response_stats",
                  "hourly_rate", "status_breakdown"]
    job_of = {}
    for sid in script_ids:
        jobs = submit(client, [{"script_id": sid, "params": {}}])
        job_of[sid] = jobs[0]["id"]
    wait_status(client, list(job_of.values()), ("completed", "failed"), timeout=120)
    for sid, jid in job_of.items():
        detail = client.get(f"/api/jobs/{jid}").json()
        assert detail["status"] == "completed", (
            f"{sid} failed in the sandbox:\n"
            f"{client.get(f'/api/jobs/{jid}/stderr').text[:300]}")
