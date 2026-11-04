"""Interactive terminal sessions over a pseudo-terminal.

The React tab gets a real interactive shell (xterm.js on one side, an
SSE output stream + input POSTs on the other); the python UI gets a
one-shot command box (run_command, no pty). Everything hides behind
config.TERMINAL_ENABLED (LOGSMITH_TERMINAL) — the router refuses with
404 while it is off, so the feature does not exist unless asked for.

A session is a forked pty running $SHELL in BASE_DIR. Output flows
master -> bounded queue -> SSE generator; input goes the other way as
raw keystroke bytes. The session outlives a dropped SSE connection: the
queue holds the newest _QUEUE_CHUNKS chunks while nobody drains and the
older ones are dropped (terminal output is ephemeral — nothing is
written to disk). Cleanup happens on explicit kill, when the shell
exits, or via the idle reaper when LOGSMITH_TERMINAL_IDLE_TIMEOUT
passes with no input and no output.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import os
import pty
import signal
import struct
import termios
import time
import uuid
import warnings

from ..config import BASE_DIR, TERMINAL_IDLE_TIMEOUT, TERMINAL_MAX_SESSIONS

_QUEUE_CHUNKS = 256   # output backlog kept per session (nobody listening)
_READ_CHUNK = 65536    # max bytes pulled from the pty per readable event
_DEAD_SESSION_TTL = 120  # seconds a finished session stays re-connectable
_REAP_INTERVAL = 15    # how often the idle reaper looks around
_WRITE_TIMEOUT = 30.0  # seconds before a stuck input write gives up


class SessionLimit(Exception):
    """Raised by create() when TERMINAL_MAX_SESSIONS is reached."""


class SessionDead(Exception):
    """Raised when writing to a session whose shell has exited."""


def _shell() -> str:
    """The shell to run: $SHELL when it exists, else bash, else sh."""
    for candidate in (os.environ.get("SHELL") or "", "/bin/bash", "/bin/sh"):
        if candidate and os.path.exists(candidate):
            return candidate
    return "/bin/sh"


class _Session:
    """One pty: the shell process on one side, the output queue on the other."""

    def __init__(self, sid: str, pid: int, fd: int, shell: str):
        self.id = sid
        self.pid = pid
        self.fd = fd
        self.shell = shell
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_CHUNKS)
        self.last_activity = time.monotonic()
        self.closed = False            # the master hit EOF — the shell is gone
        self.exit_code: int | None = None
        self.closed_at: float | None = None


class TerminalService:
    """Registry of live pty sessions; own this via the `terminal` singleton."""

    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}
        self._reaper: asyncio.Task | None = None

    # --- lifecycle -----------------------------------------------------

    def ensure_started(self) -> None:
        """Start the idle reaper once there is a loop to run it on."""
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    async def shutdown(self) -> None:
        """Kill every session and stop the reaper (app shutdown hook)."""
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        for sid in list(self.sessions):
            self.kill(sid)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(_REAP_INTERVAL)
            now = time.monotonic()
            for sid, session in list(self.sessions.items()):
                # finished: keep the exit frame deliverable for a while,
                # then forget the session entirely
                dead = (session.closed
                        and session.closed_at is not None
                        and now - session.closed_at > _DEAD_SESSION_TTL)
                # idle: a browser that vanished without killing its session
                idle = (not session.closed
                        and TERMINAL_IDLE_TIMEOUT > 0
                        and now - session.last_activity > TERMINAL_IDLE_TIMEOUT)
                if dead or idle:
                    try:
                        self.kill(sid)
                    except OSError:  # never let the reaper die
                        self.sessions.pop(sid, None)

    # --- sessions --------------------------------------------------------

    def create(self) -> _Session:
        if len(self.sessions) >= TERMINAL_MAX_SESSIONS:
            raise SessionLimit()
        shell = _shell()
        # Build the child's environment BEFORE the fork: the child path
        # below touches no allocator and no lock — just two syscalls.
        env = {**os.environ, "TERM": "xterm-256color"}
        # forkpty() warns in multi-threaded processes (a forked child can
        # deadlock on a lock another thread held). Ours execs immediately,
        # so the hazard cannot materialize.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(BASE_DIR)
                os.execve(shell, [shell], env)
            except OSError:
                os._exit(127)
        sid = uuid.uuid4().hex[:12]
        session = _Session(sid, pid, fd, shell)
        self.sessions[sid] = session
        self.ensure_started()
        asyncio.get_running_loop().add_reader(fd, self._on_readable, session)
        return session

    def get(self, sid: str) -> _Session | None:
        return self.sessions.get(sid)

    def kill(self, sid: str) -> bool:
        """End a session now: True when there was one to end."""
        session = self.sessions.pop(sid, None)
        if session is None:
            return False
        # the whole process group — killing only the shell would leave
        # whatever it was running (vim, tail -f) behind
        self._signal_group(session, signal.SIGTERM)
        if not session.closed:
            self._close(session)
        try:
            os.close(session.fd)  # also SIGHUPs anyone still attached
        except OSError:
            pass
        # a stubborn process group gets SIGKILL shortly after
        loop = asyncio.get_running_loop()
        loop.call_later(2.0, self._final_kill, session.pid)
        return True

    def _final_kill(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass  # gone already — the common case

    def _signal_group(self, session: _Session, sig: int) -> None:
        try:
            os.killpg(os.getpgid(session.pid), sig)
        except OSError:
            pass

    # --- output path -----------------------------------------------------

    def _on_readable(self, session: _Session) -> None:
        """Reader callback: the pty has bytes (or is done)."""
        try:
            data = os.read(session.fd, _READ_CHUNK)
        except BlockingIOError:
            return  # spurious wakeup, nothing to read yet
        except OSError:
            data = b""  # EIO: the slave side is closed — the shell exited
        if data:
            session.last_activity = time.monotonic()
            self._enqueue(session, data)
        else:
            self._close(session)

    def _enqueue(self, session: _Session, data: bytes | None) -> None:
        """Queue an output chunk (None = end). Bounded, drop-oldest."""
        if session.queue.full():
            try:
                session.queue.get_nowait()  # drop the oldest chunk
            except asyncio.QueueEmpty:
                pass
        session.queue.put_nowait(data)

    def _close(self, session: _Session) -> None:
        """The master hit EOF: stop reading, reap the child, wake streams."""
        if session.closed:
            return
        session.closed = True
        session.closed_at = time.monotonic()
        loop = asyncio.get_running_loop()
        try:
            loop.remove_reader(session.fd)
        except (ValueError, OSError):
            pass
        # Reap now if the child is already dead (the usual case at EOF);
        # otherwise let a thread block on it so it never turns into a zombie.
        try:
            done, status = os.waitpid(session.pid, os.WNOHANG)
            if done == session.pid:
                session.exit_code = os.waitstatus_to_exitcode(status)
            else:
                loop.run_in_executor(None, os.waitpid, session.pid, 0)
        except (ChildProcessError, OSError):
            pass
        self._enqueue(session, None)  # wake every waiting stream

    async def stream_output(self, session: _Session):
        """SSE frames for one session: output chunks, then one exit frame.

        A finished session with nothing queued answers with the exit frame
        immediately — which also ends EventSource's auto-reconnect loop.
        """
        if session.closed and session.queue.empty():
            yield _sse({"type": "exit", "code": session.exit_code})
            return
        while True:
            item = await session.queue.get()
            if item is None:
                break
            yield _sse({"type": "output",
                        "data": base64.b64encode(item).decode("ascii")})
        yield _sse({"type": "exit", "code": session.exit_code})

    # --- input path -------------------------------------------------------

    async def write(self, session: _Session, data: bytes) -> None:
        """Send keystrokes to the shell (blocking-safe: off the event loop)."""
        if session.closed:
            raise SessionDead()
        session.last_activity = time.monotonic()
        loop = asyncio.get_running_loop()
        # A pty master can block when its buffer fills (a huge paste, a
        # shell that stopped reading) — never stall the loop on that.
        await asyncio.wait_for(
            loop.run_in_executor(None, self._blocking_write, session.fd, data),
            _WRITE_TIMEOUT)

    @staticmethod
    def _blocking_write(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(fd, view)
            except BlockingIOError:
                continue
            view = view[n:]

    def resize(self, session: _Session, cols: int, rows: int) -> None:
        """Tell the pty its new size (TIOCSWINSZ + SIGWINCH to the group)."""
        if session.closed:
            raise SessionDead()
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(session.fd, termios.TIOCSWINSZ, winsize)
        self._signal_group(session, signal.SIGWINCH)
        session.last_activity = time.monotonic()

    # --- one-shot commands (python UI) -------------------------------------

    async def run_command(self, cmd: str, timeout: float = 30.0) -> dict:
        """Run one command to completion and capture its combined output.

        The python UI's command box — no pty, no interactivity, a hard
        timeout and a bounded result. The interactive shell lives in the
        React tab.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                _shell(), "-c", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=BASE_DIR,
            )
        except OSError as exc:  # no shell at all
            return {"ok": False, "output": "", "exit_code": None,
                    "error": f"could not start a shell: {exc}"}
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "output": "", "exit_code": None,
                    "error": f"timed out after {timeout:.0f}s"}
        return {"ok": proc.returncode == 0,
                "output": out.decode("utf-8", errors="replace")[:200_000],
                "exit_code": proc.returncode}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


terminal = TerminalService()
