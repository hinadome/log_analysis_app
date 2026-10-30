"""Asynchronous job execution engine.

Jobs run as separate processes (exec, no shell) with stdout/stderr streamed
to files so the UI can tail them live while the script is still running.
Scripts may produce artifact files; anything they write into the job's
outputs/ directory is exposed through the API.

Placeholders available in a manifest command template:
  {script}  absolute path of the script
  {log}     absolute path of the input: the selected log file, or in chain
            mode the previous step's output. Compressed inputs (.gz/.bz2/.xz
            — either the log or a chained output) are materialized as a
            decompressed copy in the job directory
  {output}  path of the job's output artifact file (output_filename)
  {args}    CLI flags rendered from validated parameters

Environment passed to every script:
  JOB_ID, JOB_LOG_PATH, JOB_OUTPUT_DIR, JOB_ORIGINAL_LOG
  (+ the server's environment)
"""
from __future__ import annotations

import asyncio
import bz2
import gzip
import lzma
import os
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import MAX_CONCURRENT_JOBS
from ..models import Job, ScriptInfo
from .job_store import JobStore, utcnow
from .scanner import discover_scripts

_READ_CHUNK = 65536

_DECOMPRESSORS = {
    ".gz": gzip.open,
    ".bz2": bz2.open,
    ".xz": lzma.open,
}


def _is_compressed(path: str) -> bool:
    return Path(path).suffix.lower() in _DECOMPRESSORS


def _find_script(script_id: str) -> ScriptInfo | None:
    for script in discover_scripts():
        if script.id == script_id:
            return script
    return None


def render_command(script: ScriptInfo, log_path: str, params: dict,
                   output_path: Path | None) -> list[str]:
    """Build the final argv for a job from the manifest command template."""
    argv: list[str] = []
    for spec in script.parameters:
        value = params.get(spec.name, spec.default)
        if value is None or value is False or value == "" or value == []:
            continue
        if spec.type == "boolean":
            argv.append(spec.flag)
        elif spec.type == "multi":
            for item in value:
                argv.extend([spec.flag, str(item)])
        else:
            argv.extend([spec.flag, str(value)])

    command: list[str] = []
    for token in shlex.split(script.command):
        token = token.replace("{script}", script.script_path)
        token = token.replace("{log}", log_path)
        if output_path is not None:
            token = token.replace("{output}", str(output_path))
        if token == "{args}":
            command.extend(argv)
        elif "{args}" in token:
            command.append(token.replace("{args}", " ".join(argv)))
        elif token:
            command.append(token)
    return command


class Executor:
    """Runs jobs with bounded concurrency and live output streaming."""

    def __init__(self, store: JobStore) -> None:
        self._store = store
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self._tasks: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancelling: set[str] = set()
        # Chains with a cancellation in flight. The flag is set before any
        # killing starts so that a step completing mid-cancel cannot hand
        # off to the next one (_advance_chain checks it). Chain ids are
        # unique per submission, so entries never need removing.
        self._cancelling_chains: set[str] = set()
        self._shutting_down = False

    # -- public API -------------------------------------------------------
    def start(self) -> None:
        """Reset per-process state; called from the application lifespan.

        Production processes are fresh anyway, but re-entering the app
        (e.g. one TestClient per test) must not inherit a shutdown state.
        """
        self._shutting_down = False

    def submit(self, job: Job, script: ScriptInfo) -> None:
        """Render the command, persist it, and schedule the job.

        Compressed inputs (.gz/.bz2/.xz logs *and* compressed outputs of a
        previous chain step) are referenced as a decompressed copy
        (job_dir/input.log) that _run materializes before the script starts.
        """
        out_dir = self._store.outputs_dir(job.id)
        output_path = None
        if "{output}" in script.command:
            output_path = out_dir / (script.output_filename or "output.txt")
            job.output_file = str(output_path)
        log_arg = str(self.input_path(job))
        job.command = render_command(script, log_arg, job.params, output_path)
        self._store.save(job)
        self._tasks[job.id] = asyncio.create_task(self._run(job, script))

    def submit_chain(self, steps: list[tuple[Job, ScriptInfo]], chain_id: str,
                     start_index: int = 0, total: int | None = None) -> None:
        """Create a chain: the first step runs now, the rest wait for their turn.

        start_index/total preserve the original step numbering when only part
        of a chain is re-run (continue-from-step-N): the UI then shows e.g.
        "step 3/5" even though only two steps are in this chain. The first
        step may carry an input_path (seeded from a persisted earlier output),
        in which case it does not read the selected log.
        """
        size = total if total is not None else len(steps)
        for i, (job, script) in enumerate(steps):
            job.chain_id = chain_id
            job.chain_index = start_index + i
            job.chain_size = size
            if i > 0:
                job.status = "waiting"
            elif not job.input_path:
                job.input_source = "log"
            self._store.save(job)
        self.submit(steps[0][0], steps[0][1])

    def input_path(self, job: Job) -> Path:
        """Path scripts should read: chained input, decompressed copy, or the log.

        Both the selected log and a previous step's chained output are
        decompressed into job_dir/input.log when they are .gz/.bz2/.xz —
        a compressed chain output must never be piped raw into a script.
        """
        source = job.input_path or job.log_path
        if _is_compressed(source):
            return self._store.job_dir(job.id) / "input.log"
        return Path(source)

    def _materialize_input(self, job: Job) -> None:
        """Decompress a compressed input into job_dir/input.log if needed."""
        source = Path(job.input_path or job.log_path)
        opener = _DECOMPRESSORS.get(source.suffix.lower())
        if opener is None:
            return
        target = self._store.job_dir(job.id) / "input.log"
        if target.exists():
            return
        with opener(source, "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, _READ_CHUNK)

    # -- chain advancement -------------------------------------------------
    def chain_output(self, job: Job) -> tuple[Path | None, str | None]:
        """Resolve the file a completed job passes to the next chain step."""
        # 1) declared artifact ({output} placeholder) if it exists
        if job.output_file and Path(job.output_file).is_file():
            return Path(job.output_file), "prev_output"
        # 2) the single file the script wrote into outputs/
        out_dir = self._store.outputs_dir(job.id)
        if out_dir.is_dir():
            files = sorted(p for p in out_dir.iterdir() if p.is_file())
            if len(files) == 1:
                return files[0], "prev_output"
        # 3) fall back to the step's stdout
        stdout = self._store.stdout_path(job.id)
        if stdout.is_file() and stdout.stat().st_size > 0:
            return stdout, "prev_stdout"
        return None, None

    def _advance_chain(self, job: Job) -> None:
        """After a successful step, submit the next one with this step's output."""
        if job.chain_id in self._cancelling_chains:
            # A cancel raced with this step completing — never hand off.
            self._skip_remaining(job, reason="Cancelled by user")
            return
        nxt = self._next_waiting_step(job)
        if nxt is None:
            return  # last step, or the remaining ones are not runnable
        input_path, source = self.chain_output(job)
        script = _find_script(nxt.script_id)
        if input_path is None:
            nxt.status = "failed"
            nxt.error = "Previous step produced no output to chain"
            nxt.finished_at = utcnow()
            self._store.save(nxt)
            self._skip_remaining(nxt)
            return
        if script is None or script.error:
            nxt.status = "failed"
            nxt.error = f"Script no longer available: {nxt.script_id}"
            nxt.finished_at = utcnow()
            self._store.save(nxt)
            self._skip_remaining(nxt)
            return
        nxt.input_path = str(input_path)
        nxt.input_source = source
        self.submit(nxt, script)

    def _next_waiting_step(self, job: Job) -> Job | None:
        """Next surviving waiting step after this one, by step index.

        Tolerates gaps: if a middle step was deleted, the chain continues
        with the next one that still exists instead of stalling forever.
        """
        for step in self._store.list_chain(job.chain_id):  # index-ordered
            if step.chain_index > job.chain_index and step.status == "waiting":
                return step
        return None

    def _skip_remaining(self, job: Job, reason: str | None = None) -> None:
        """Mark all later steps of a chain as skipped."""
        for step in self._store.list_chain(job.chain_id):
            if step.chain_index > job.chain_index and step.status == "waiting":
                step.status = "skipped"
                step.error = reason or f"Skipped: step {job.chain_index + 1} did not complete"
                step.finished_at = utcnow()
                self._store.save(step)

    async def cancel_chain(self, chain_id: str) -> bool:
        """Cancel a whole chain: kill the active step, skip the waiting ones.

        The cancellation flag is set *before* anything is killed so that a
        step completing mid-cancel cannot advance the chain — _advance_chain
        checks the flag and skips the remaining steps instead.
        """
        self._cancelling_chains.add(chain_id)
        touched = False
        # Kill active steps — including one that was just submitted but has
        # not flipped from "waiting" to "running" yet (it has a live task).
        for step in self._store.list_chain(chain_id):
            if step.status in ("queued", "running") or step.id in self._tasks:
                await self.cancel(step.id)
                touched = True
        # Steps that never got a task simply flip to skipped on disk.
        for step in self._store.list_chain(chain_id):
            if step.status == "waiting":
                step.status = "skipped"
                step.error = "Cancelled by user"
                step.finished_at = utcnow()
                self._store.save(step)
                touched = True
        return touched

    async def cancel(self, job_id: str) -> bool:
        job = self._store.get(job_id)
        if job is None:
            return False
        task = self._tasks.get(job_id)
        # A "waiting" chain step can already own a task in the brief window
        # between submit() and the task's first execution — cancel it too.
        if job.status not in ("queued", "running") and (task is None or task.done()):
            return False
        self._cancelling.add(job_id)
        proc = self._procs.get(job_id)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        elif task is not None and not task.done():
            task.cancel()
        return True

    async def shutdown(self) -> None:
        # Set before any cancellation so _run's finally block stops doing
        # chain bookkeeping: leftover waiting steps are marked interrupted
        # by reconcile() on the next start, and no new tasks are spawned
        # after shutdown has already gathered.
        self._shutting_down = True
        for proc in self._procs.values():
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals ---------------------------------------------------------
    async def _run(self, job: Job, script: ScriptInfo) -> None:
        job_dir = self._store.job_dir(job.id)
        out_dir = self._store.outputs_dir(job.id)
        stdout_f = stderr_f = None
        try:
            async with self._semaphore:
                if job.id in self._cancelling:
                    raise asyncio.CancelledError

                job.status = "running"
                job.started_at = utcnow()
                self._store.save(job)

                try:
                    self._materialize_input(job)
                except Exception as exc:
                    job.status = "failed"
                    job.error = f"Failed to decompress input: {exc}"
                    return

                input_path = self.input_path(job)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *job.command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=str(out_dir),
                        env={
                            **os.environ,
                            "JOB_ID": job.id,
                            "JOB_LOG_PATH": str(input_path),
                            "JOB_ORIGINAL_LOG": job.log_path,
                            "JOB_OUTPUT_DIR": str(out_dir),
                        },
                    )
                except Exception as exc:
                    job.status = "failed"
                    job.error = f"Failed to start: {exc}"
                    return

                self._procs[job.id] = proc
                stdout_f = (job_dir / "stdout.txt").open("wb")
                stderr_f = (job_dir / "stderr.txt").open("wb")

                async def pump(stream, handle) -> None:
                    while True:
                        chunk = await stream.read(_READ_CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        handle.flush()

                timed_out = False
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            pump(proc.stdout, stdout_f),
                            pump(proc.stderr, stderr_f),
                        ),
                        timeout=script.timeout,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

                exit_code = await proc.wait()
                job.exit_code = exit_code

                if job.id in self._cancelling:
                    job.status = "cancelled"
                    job.error = "Cancelled by user"
                elif timed_out:
                    job.status = "failed"
                    job.error = f"Timed out after {script.timeout} seconds"
                elif exit_code == 0:
                    job.status = "completed"
                else:
                    job.status = "failed"
                    job.error = f"Exited with code {exit_code}"

        except asyncio.CancelledError:
            proc = self._procs.get(job.id)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, Exception):
                    pass
            job.status = "cancelled"
            job.error = ("Cancelled by server shutdown" if self._shutting_down
                         else "Cancelled by user")

        finally:
            for handle in (stdout_f, stderr_f):
                if handle is not None:
                    handle.close()
            job.finished_at = utcnow()
            job.stdout_size = self._file_size(self._store.stdout_path(job.id))
            job.stderr_size = self._file_size(self._store.stderr_path(job.id))
            job.output_files = self._collect_outputs(out_dir)
            self._store.save(job)
            self._procs.pop(job.id, None)
            self._cancelling.discard(job.id)
            self._tasks.pop(job.id, None)
            # Chain bookkeeping: advance on success, skip the rest on failure.
            # During shutdown, leave states alone — reconcile() marks any
            # unfinished steps as interrupted on the next server start.
            if job.chain_id and not self._shutting_down:
                if job.status == "completed":
                    try:
                        self._advance_chain(job)
                    except Exception as exc:  # never let bookkeeping kill the runner
                        print(f"chain advance error for {job.id}: {exc}")
                        self._skip_remaining(job)
                elif job.status in ("failed", "cancelled"):
                    self._skip_remaining(job)

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _collect_outputs(out_dir: Path) -> list[str]:
        if not out_dir.is_dir():
            return []
        return sorted(p.name for p in out_dir.iterdir() if p.is_file())
