"""This turn's diff, without touching the person's index, stash or files.

Why snapshots: ``git diff HEAD`` at the end of a turn would blame the agent
for every earlier turn's changes and for the person's own edits. So before
the agent runs, the client records the working tree as a tree object, using
a private index file (``GIT_INDEX_FILE``) seeded from a copy of the real one
so only changed files are hashed. At the end of the turn it takes a second
snapshot the same way and diffs the two trees. Credential files are left out
of both snapshots and of the diff with ``:(exclude)`` pathspecs.

Every git call has a timeout (within the hook's one deadline) and a size
cap, runs with ``GIT_OPTIONAL_LOCKS=0`` so a ``git diff`` never rewrites the
real index's stat cache, never fetches (a partial clone's missing objects
stay missing), and ignores any ``GIT_*`` variables the agent's environment
carries. A repo whose ``.git`` points somewhere else (a ``gitdir:`` file or
``core.worktree``) gets no diff, except a linked worktree its main repo
lists. Submodules show as one commit line, never their content.

The snapshot writes loose objects (blobs and trees) into the repo's own
``.git/objects``; nothing references them, so ``git gc`` removes them.
"""

from __future__ import annotations

import atexit
import contextlib
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import MAX_DIFF_CHARS
from .forbidden import exclude_pathspecs, is_forbidden_path

GIT_TIMEOUT_S = 3.0
# Windows has no process groups to kill, no signal masks and no
# ``preexec_fn``: git runs in a new process group and is killed alone. A
# module value, so tests can take either branch on any OS.
WINDOWS = os.name == "nt"
GIT_COMMAND: tuple[str, ...] = ("git",)  # git from PATH, as the agent runs it
TRUNCATED = "\n[promptcoach: diff truncated]\n"
_DIFF_BYTES = MAX_DIFF_CHARS * 4  # UTF-8 never needs more for the cap
_SMALL_BYTES = 1_000_000  # rev-parse, write-tree, ls-files
_MIN_STEP_S = 0.05  # less time than this left: a git call can't finish
_NO_DEADLINE_S = 60.0  # for callers without a hook budget (tests, tools)
# Overrides for repo-local settings that would make git run a program or go
# online: an fsmonitor hook, hooks, automatic maintenance, any transport (a
# partial clone fetches missing objects through core.sshCommand), a
# submodule diff format that shows content. Filters are handled by
# ``_repo_defines_filters`` and diff drivers by --no-ext-diff/--no-textconv.
_SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.allow=never",
    "-c",
    "diff.submodule=short",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)
_TRUSTED_SCOPES = frozenset({"global", "system"})
_DIFF_FLAGS = (
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--submodule=short",
    "--ignore-submodules=dirty",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


class Deadline:
    """The end of a hook's time budget, on the monotonic clock.

    One per hook run: every git call and the request get what is left of
    it (capped at their own limit), and a step that would start after it
    is skipped, so the hook as a whole ends on time.
    """

    def __init__(self, seconds: float, start: float | None = None) -> None:
        """``seconds`` from ``start`` (a ``time.monotonic()`` reading; now if None)."""
        self.end = (time.monotonic() if start is None else start) + seconds

    def left(self, cap: float = math.inf) -> float:
        """Seconds left, at most ``cap``; never negative."""
        return max(min(cap, self.end - time.monotonic()), 0.0)

    def spent(self) -> bool:
        """True once (almost) nothing is left: don't start anything new."""
        return self.left() < _MIN_STEP_S


@dataclass(frozen=True)
class Area:
    """Where a diff may look: the project's own directory, never a home repo.

    A project can sit inside a bigger repo (a monorepo, or a dotfiles repo
    holding the person's whole home): only the project's subtree is
    snapshotted and diffed, and a repo whose top is the home directory, or
    above it, gets no diff at all.
    """

    project_dir: Path
    home: Path


# Process groups of the git calls running now, killed if the hook is stopped
# (SIGTERM from the agent at its timeout) or exits with one still going.
_LIVE: set[int] = set()
_PREVIOUS_TERM: Any = signal.SIG_DFL
_CLEANUP_INSTALLED: list[bool] = []
# Threads whose SIGTERM came while git was starting there (the signal blocked):
# run_git hands it back to the handler as soon as git is in _LIVE.
_PENDING_TERM: set[int] = set()


# Held while git starts and is recorded in _LIVE, so the SIGTERM handler
# (which runs in the main thread) and the exit cleanup can't run in between
# and miss it. Reentrant: a SIGTERM handled during the exit cleanup takes it
# again in the same thread, and mustn't deadlock.
_STARTING = threading.RLock()

# Set by the exit cleanup. From then on no git starts, and a thread done with
# git parks instead of returning: the cleanup's kill wakes run_git's thread
# and its reader just as the interpreter finalizes, and CPython 3.9 can crash
# (SIGSEGV) when a daemon thread takes the GIL then. _BUSY counts the threads
# inside run_git (readers included), so the cleanup can wait until each one
# has parked. A parked thread waits on _PARKED, which shares _BUSY_CHANGED's
# lock but which no decrement notifies, so one thread's decrement never wakes
# another that has parked. _RESUME is never set in a hook; tests set it (and
# notify _PARKED) to let threads go.
_EXITING = threading.Event()
_RESUME = threading.Event()
_BUSY: list[int] = [0]
_BUSY_LOCK = threading.RLock()
_BUSY_CHANGED = threading.Condition(_BUSY_LOCK)
_PARKED = threading.Condition(_BUSY_LOCK)

# A git whose start (Popen) hasn't returned isn't in _LIVE yet, and if the
# start outlasts the exit cleanup's wait, its thread may never run again:
# the process can end first, leaving git orphaned (fix round 3). So each git
# child, between fork and exec (preexec_fn, POSIX), reports its own pid on
# _STARTED and then checks _EXIT_BEGUN; the cleanup writes _EXIT_BEGUN
# before it reads _STARTED. Pipes, so every step is a system call, in order:
# a pid reported before the cleanup's read is killed by the cleanup; one
# reported after it sees the exit flag and the child ends before exec, so
# no git runs. run_git reads its own pid back (under _STARTING, after
# putting git in _LIVE), so the cleanup only ever finds a start still in
# progress, whose pid can't have been reused (it isn't reaped). Both ends
# are non-blocking and close on exec: git inherits neither.
_PARENT_PID = os.getpid()
_STARTED_R = _STARTED_W = _EXIT_BEGUN_R = _EXIT_BEGUN_W = -1
if os.name != "nt":  # pragma: no branch - Windows has no preexec_fn
    _STARTED_R, _STARTED_W = os.pipe()
    _EXIT_BEGUN_R, _EXIT_BEGUN_W = os.pipe()
    for _fd in (_STARTED_R, _STARTED_W, _EXIT_BEGUN_R, _EXIT_BEGUN_W):
        os.set_blocking(_fd, False)
_EXIT_FLAG = b"x" * 512  # one byte per child that starts after it; few ever do


def _drain(fd: int) -> bytes:
    """Everything waiting in a non-blocking pipe (none: b"")."""
    data = b""
    if fd < 0:
        return data
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # BlockingIOError: empty
            return data
        if not chunk:
            return data
        data += chunk


def _unreturned_starts() -> list[int]:
    """Pids of gits started (forked, past setsid) whose start hasn't returned."""
    return [int(word) for word in _drain(_STARTED_R).split()]


def _in_git_child() -> None:
    """preexec_fn: in git's child, after setsid and before exec. SIGTERM is
    unblocked (an orphan still obeys a later SIGTERM); the pid is reported;
    and if the hook has begun to exit, the child ends here, before git runs.

    Not only system calls: it builds a small bytes object (the pid line) and
    raises and catches BlockingIOError (an empty flag pipe). That is safe in
    CPython's child after a fork: the forking thread holds the GIL, so no
    other thread's Python state is mid-change; the objects are small, so
    they come from pymalloc, which the GIL guards; and nothing is imported.
    The earlier preexec_fn, functools.partial(pthread_sigmask, ...), already
    allocated in the child the same way."""
    signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGTERM,))
    if os.getpid() == _PARENT_PID:  # called in process (a test): not a child
        return
    with contextlib.suppress(OSError):
        os.write(_STARTED_W, b"%d\n" % os.getpid())
    try:
        begun = os.read(_EXIT_BEGUN_R, 1)
    except OSError:  # BlockingIOError: the hook isn't exiting
        return
    if begun:
        os._exit(126)


class _WinJobs:  # pragma: no cover - Windows only; test_real_job_object_kills_a_process_tree
    """Windows Job Objects through ctypes: a job set to kill every process in
    it when its last handle closes, so git, and any git that a wrapper
    ``git.exe`` started, ends with the job (or with the hook, if the hook is
    killed outright and Windows closes its handles). Every call fails soft:
    ``create`` returns None and the caller falls back to ``taskkill``.
    """

    def _kernel32(self) -> Any:
        import ctypes

        return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    def create(self) -> int | None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        try:
            k32 = self._kernel32()
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            # 9: JobObjectExtendedLimitInformation
            if not k32.SetInformationJobObject(
                wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                k32.CloseHandle(wintypes.HANDLE(job))
                return None
            return int(job)
        except Exception:  # noqa: BLE001 - no ctypes, no kernel32: fall back
            return None

    def assign(self, job: int, proc: subprocess.Popen[bytes]) -> bool:
        from ctypes import wintypes

        try:
            handle = wintypes.HANDLE(int(proc._handle))  # type: ignore[attr-defined]
            return bool(self._kernel32().AssignProcessToJobObject(wintypes.HANDLE(job), handle))
        except Exception:  # noqa: BLE001
            return False

    def terminate(self, job: int) -> None:
        from ctypes import wintypes

        with contextlib.suppress(Exception):
            self._kernel32().TerminateJobObject(wintypes.HANDLE(job), 1)

    def close(self, job: int) -> None:
        from ctypes import wintypes

        with contextlib.suppress(Exception):
            self._kernel32().CloseHandle(wintypes.HANDLE(job))


_JOBS: Any = _WinJobs()  # tests put a fake here
_JOB_OF: dict[int, int] = {}  # git's pid -> its job, on Windows


def _run_taskkill(argv: list[str], timeout: float) -> None:
    """``taskkill /T /F``: the fallback when no job could be made. Never raises."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - a fixed system tool and a pid
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )


def _kill_pid(pid: int) -> None:
    """Kill a git and what it started: its process group on POSIX; on
    Windows its job, else ``taskkill /T`` and TerminateProcess."""
    if WINDOWS:
        job = _JOB_OF.get(pid)
        if job is not None:
            _JOBS.terminate(job)
            return
        _run_taskkill(["taskkill", "/T", "/F", "/PID", str(pid)], 1.0)
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)  # TerminateProcess
        return
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)


def _kill_live() -> None:
    """Kill every git process group still running."""
    for pgid in list(_LIVE):
        _kill_pid(pgid)
        _LIVE.discard(pgid)


_READER_GRACE_S = 0.2  # after a kill, how long to wait for git's output to close
_EXIT_WAIT_S = 0.3  # how long the exit cleanup waits for a git that is starting


def _exit_cleanup() -> None:
    """At exit: kill the running git, waiting briefly for one that is just
    starting, then for every git thread to park.

    A git start (Popen) stalled past the 0.3 s doesn't hold the hook's exit
    (p32). Its git is killed anyway: the child reported its pid before exec
    (_in_git_child), and the cleanup kills every start that hasn't returned;
    a child that reports after that ends before exec. The stalled thread is
    still counted when the cleanup returns; if it runs again before the
    process ends, it finds the hook exiting and parks.
    """
    wait_until = time.monotonic() + _EXIT_WAIT_S
    # Under _BUSY_CHANGED, so a git thread's last critical section sees
    # either "not exiting" (and returns) or "exiting" (and parks), never a
    # change in the middle of it.
    with _BUSY_CHANGED:
        _EXITING.set()
    if _EXIT_BEGUN_W >= 0:  # before reading _STARTED: see _in_git_child
        with contextlib.suppress(OSError):
            os.write(_EXIT_BEGUN_W, _EXIT_FLAG)
    acquired = _STARTING.acquire(timeout=_EXIT_WAIT_S)
    try:
        for pid in _unreturned_starts():
            _kill_pid(pid)
        _kill_live()
    finally:
        if acquired:
            _STARTING.release()
    with _BUSY_CHANGED:
        _BUSY_CHANGED.wait_for(lambda: not _BUSY[0], max(wait_until - time.monotonic(), 0.0))


def _park() -> None:
    """Never return (unless a test sets _RESUME): wait on a lock, outside
    the GIL, with every signal blocked, so this thread runs no Python while
    the interpreter shuts down."""
    if not WINDOWS:
        signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
    _RESUME.wait()


@contextlib.contextmanager
def _git_thread() -> Iterator[None]:
    """Count this thread as busy with git; when it is done, it returns as
    usual, or parks if the exit cleanup has begun.

    Parking happens in one critical section: every signal is blocked while
    the thread is still counted, then the count drops, and the thread waits
    on _PARKED (same lock), which releases the lock and sleeps in one step.
    So the cleanup, which returns once the count is 0, never returns while
    this thread can still run Python with signals open. And since nothing in
    a hook notifies _PARKED, a later decrement (run_git's thread, after its
    reader parked) wakes only the cleanup, never a parked thread.
    """
    with _BUSY_CHANGED:
        _BUSY[0] += 1
    try:
        yield
    finally:
        with _BUSY_CHANGED:
            exiting = _EXITING.is_set()
            if exiting and not WINDOWS:
                signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
            _BUSY[0] -= 1
            _BUSY_CHANGED.notify_all()
            if exiting:
                # Woken only by a test that sets _RESUME and notifies _PARKED.
                _PARKED.wait_for(_RESUME.is_set)


def _on_term(signum: int, frame: object) -> None:
    """SIGTERM: kill the running git first, then do what SIGTERM did before.

    Python runs this handler late, at its next check: a SIGTERM that came just
    before run_git blocked it can land inside git's start, in this thread.
    Then it waits, and run_git runs it again once git is in _LIVE.
    """
    if not WINDOWS and signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []):
        _PENDING_TERM.add(threading.get_ident())
        return
    with _STARTING:
        _kill_live()
    if callable(_PREVIOUS_TERM):
        _PREVIOUS_TERM(signum, frame)
        return
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def install_cleanup() -> None:
    """Make sure no git outlives the hook (call once, from the main thread)."""
    global _PREVIOUS_TERM
    if _CLEANUP_INSTALLED:
        return
    _CLEANUP_INSTALLED.append(True)
    atexit.register(_exit_cleanup)
    _PREVIOUS_TERM = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _on_term)


@dataclass(frozen=True)
class GitOutput:
    """What a git command printed, and whether the size cap cut it short."""

    text: str
    truncated: bool


def _env(index_file: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        LC_ALL="C",
    )
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    return env


def _start_options(windows: bool) -> dict[str, Any]:
    """How git is started: in its own session, through _in_git_child
    (POSIX), or in a new process group (Windows, which has neither)."""
    if windows:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True, "preexec_fn": _in_git_child}


def run_git(
    args: list[str],
    cwd: Path,
    *,
    timeout: float = GIT_TIMEOUT_S,
    limit: int = _SMALL_BYTES,
    index_file: Path | None = None,
) -> GitOutput | None:
    """Run ``git`` in ``cwd``; None on any failure, non-zero exit or timeout.

    Output beyond ``limit`` bytes isn't read: git is stopped and the result is
    marked truncated, so a huge diff can't fill memory. Git runs in its own
    process group, and a stop kills the whole group: a program git started
    can't outlive it or hold its output open. Once the hook is exiting, no git
    starts, and the calling thread parks rather than return (_EXITING).
    """
    if _EXITING.is_set():
        _park()
        return None
    with _git_thread():
        return _run_git(args, cwd, timeout, limit, index_file)


def _run_git(
    args: list[str], cwd: Path, timeout: float, limit: int, index_file: Path | None
) -> GitOutput | None:
    if timeout < _MIN_STEP_S:
        return None
    # SIGTERM is blocked in this thread, and the handler kept out by the
    # lock, from git's start until it is in _LIVE: a stop then always finds
    # it. git itself gets SIGTERM unblocked, so an orphan (a hook killed with
    # SIGKILL) still obeys a later SIGTERM.
    windows = WINDOWS
    mask = None if windows else signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        with _STARTING:
            try:
                # git from PATH, as the agent runs it; the arguments are ours alone.
                proc = subprocess.Popen(  # noqa: S603
                    [*GIT_COMMAND, "--no-pager", *_SAFE_CONFIG, *args],
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=_env(index_file),
                    **_start_options(windows),
                )
                _LIVE.add(proc.pid)
                if windows:
                    _add_job(proc)
            finally:
                # In _LIVE first, then taken back from _STARTED: the exit
                # cleanup finds this git in one or the other.
                _unreturned_starts()
    except OSError:  # git missing, cwd gone
        return None
    finally:
        if mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if threading.get_ident() in _PENDING_TERM:
            _PENDING_TERM.discard(threading.get_ident())
            _on_term(signal.SIGTERM, None)
    if _EXITING.is_set():  # the exit cleanup ran while git started: it can't miss it
        _kill(proc)
    chunks: list[bytes] = []
    stdout = proc.stdout
    assert stdout is not None  # noqa: S101 - PIPE always sets it

    # Set when the read is over: the reader may then park rather than end.
    finished = threading.Event()

    def read() -> None:
        with _git_thread():
            try:
                with contextlib.suppress(Exception):  # a thread's error would print to stderr
                    chunks.append(stdout.read(limit + 1))
            finally:
                stdout.close()  # here too: a read still blocked at the end is closed when it ends
                finished.set()

    reader = threading.Thread(target=read, daemon=True)
    deadline = time.monotonic() + timeout
    reader.start()
    finished.wait(timeout)
    truncated = bool(chunks) and len(chunks[0]) > limit
    try:
        if not finished.is_set() or truncated:
            _kill(proc)
            # The pipe closes with git's group (POSIX) or job; if something
            # still holds it (a wrapper's child on Windows), stop waiting.
            finished.wait(_READER_GRACE_S)
        code = proc.wait(max(deadline - time.monotonic(), 0.1))
    except subprocess.TimeoutExpired:  # git closed its output but kept running
        _kill(proc)
        proc.wait()
        return None
    finally:
        if finished.is_set():  # closing under a blocked read would wait for it
            stdout.close()
        _LIVE.discard(proc.pid)
        job = _JOB_OF.pop(proc.pid, None)
        if job is not None:
            _JOBS.close(job)  # kills whatever is left in it
    if not chunks or (code != 0 and not truncated):
        return None
    return GitOutput(chunks[0][:limit].decode("utf-8", errors="replace"), truncated)


def _add_job(proc: subprocess.Popen[bytes]) -> None:
    """Put a just-started git in a kill-on-close job (Windows)."""
    job = _JOBS.create()
    if job is None:
        return
    if _JOBS.assign(job, proc):
        _JOB_OF[proc.pid] = job
    else:
        _JOBS.close(job)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill git and everything it started (its process group, or its job)."""
    _kill_pid(proc.pid)
    proc.kill()


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve(strict=True) == b.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


# The git dir is walked whole before any diff: bounded, failing closed.
_MAX_GIT_DIR_ENTRIES = 100_000
_GIT_DIR_WALK_S = 0.3


def _no_symlinks(git_dir: Path, deadline: Deadline) -> bool:
    """True if everything in ``git_dir`` is a plain file or directory: no
    symlink, FIFO, socket or device (an lstat walk that never follows a
    link), within 100,000 entries and 0.3 s of the hook's budget; False when
    it can't tell (too big, too slow, unreadable).

    Why one walk and not a list of entries: refs, objects (and each fan-out
    directory or pack), the index, info/grafts, shallow... any of them can
    be a symlink into another of the person's repos, and git would serve
    that repo's history as this one's; a FIFO would hang whoever reads it.
    ``hooks/`` is skipped: hooks are often symlinked into the project, and
    git never runs them here (``core.hooksPath`` is forced to /dev/null).
    Hardlinked files are out of scope: objects are content-addressed, so a
    hardlink can only supply an object the attacker already knows.
    """
    stop = min(time.monotonic() + _GIT_DIR_WALK_S, deadline.end)
    seen = 0
    try:
        if git_dir.is_symlink() or not git_dir.is_dir():
            return False
        pending = [str(git_dir)]
        while pending:
            top = pending.pop()
            with os.scandir(top) as entries:
                for entry in entries:
                    if top == str(git_dir) and entry.name == "hooks":
                        continue
                    seen += 1
                    if seen > _MAX_GIT_DIR_ENTRIES or time.monotonic() > stop:
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif not entry.is_file(follow_symlinks=False):
                        return False  # a symlink, FIFO, socket or device
    except OSError:
        return False
    return True


_CONTROL_BYTES = 4096  # gitdir, commondir, alternates: a path or a few


def _read_control(path: Path) -> tuple[bool, str]:
    """A small git control file the plugin reads itself: (ok, text).

    Missing is fine (``(True, "")``). Anything but a regular file of at most
    4 KB of UTF-8 (a FIFO, a symlink, a directory, a huge file) is
    ``(False, "")``: the caller then refuses, so nothing can make the hook
    block on a read.
    """
    nofollow, nonblock = getattr(os, "O_NOFOLLOW", None), getattr(os, "O_NONBLOCK", None)
    checked = None
    try:
        # Windows has no such flags (and no FIFOs): refuse a symlink, or
        # anything else not a regular file, before opening it, and then
        # make sure the file opened is the one checked.
        if nofollow is None or nonblock is None:
            checked = os.lstat(path)
            if not stat.S_ISREG(checked.st_mode):
                return False, ""
        flags = os.O_RDONLY | (nofollow or 0) | (nonblock or 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return True, ""
    except OSError:
        return False, ""
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return False, ""
        if checked is not None and not os.path.samestat(checked, opened):
            return False, ""
        data = os.read(fd, _CONTROL_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _CONTROL_BYTES:
        return False, ""
    try:
        return True, data.decode("utf-8")
    except UnicodeDecodeError:
        return False, ""


def _borrows(git_dir: Path) -> bool:
    """True if a git dir takes refs or objects from elsewhere: a ``commondir``
    file, or a non-empty (or unreadable) ``objects/info/alternates``."""
    if os.path.lexists(git_dir / "commondir"):
        return True
    ok, alternates = _read_control(git_dir / "objects" / "info" / "alternates")
    return not ok or bool(alternates.strip())


def _own_git_dir(root: Path, git_dir: Path, common_dir: Path, deadline: Deadline) -> bool:
    """True if ``git_dir`` is the work tree's own: ``<root>/.git`` holding its
    own refs and objects, or a linked worktree's directory whose main repo
    lists ``<root>`` as that worktree.

    Why: a ``.git`` file (``gitdir: /elsewhere``), ``core.worktree``, or a
    ``.git`` directory with a ``commondir`` file or ``alternates`` in an
    unpacked archive, or symlinks as or inside the git dir (see
    ``_no_symlinks``), could point git at another of the person's repos, and
    its committed files would then go out as this turn's diff.
    """
    if (root / ".git").is_symlink():
        return False
    if _same(git_dir, root / ".git"):
        # The walk comes first: it refuses a FIFO before anything reads it.
        return (
            _no_symlinks(git_dir, deadline) and _same(common_dir, git_dir) and not _borrows(git_dir)
        )
    worktrees = common_dir / "worktrees"
    if not _same(git_dir.parent, worktrees):
        return False
    if not (_no_symlinks(git_dir, deadline) and _no_symlinks(common_dir, deadline)):
        return False
    ok, listed = _read_control(git_dir / "gitdir")
    if not ok or not listed.strip() or _borrows(common_dir):
        return False
    return _same(Path(listed.strip()), root / ".git")


def repo_root(cwd: Path, deadline: Deadline | None = None) -> Path | None:
    """The top of the work tree containing ``cwd``; None outside a repo, or
    when the repo's git directory isn't its own (see ``_own_git_dir``)."""
    deadline = deadline or Deadline(_NO_DEADLINE_S)
    out = run_git(
        [
            "rev-parse",
            "--show-toplevel",
            "--absolute-git-dir",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        cwd,
        timeout=deadline.left(GIT_TIMEOUT_S),
    )
    lines = out.text.splitlines() if out else []
    if len(lines) != 3 or not all(lines):
        return None
    root, git_dir, common_dir = (Path(line) for line in lines)
    return root if _own_git_dir(root, git_dir, common_dir, deadline) else None


def _repo_defines_filters(root: Path, timeout: float = GIT_TIMEOUT_S) -> bool:
    """True if the repo's own config defines a clean/smudge filter (or we can't tell).

    Why: ``git add`` and a work-tree ``git diff`` run a filter's program on
    file contents, and a repo's own ``.git/config`` (unlike the person's
    global config) may come from someone else, e.g. an unpacked archive.
    Such a repo gets no diff at all rather than a program run.
    """
    out = run_git(["config", "--show-scope", "--list"], root, timeout=timeout)
    if out is None:
        return True
    for line in out.text.splitlines():
        scope, _, entry = line.partition("\t")
        if entry.lower().startswith("filter.") and scope not in _TRUSTED_SCOPES:
            return True
    return False


_GLOB_SPECIAL = re.compile(r"([*?\[\]\\])")


def _scope(prefix: str = "") -> list[str]:
    """Pathspecs: the project's subtree (or the whole tree), credential files out.

    The excludes start under the subtree too: with a subtree include and
    top-level ``**/`` excludes, ``git add -A`` (2.50) skips untracked files.
    """
    if not prefix:
        return ["--", ".", *exclude_pathspecs()]
    escaped = _GLOB_SPECIAL.sub(r"\\\1", prefix)
    return ["--", f":(top,literal){prefix}", *exclude_pathspecs(escaped)]


def _target(cwd: Path, deadline: Deadline, area: Area | None) -> tuple[Path, list[str]] | None:
    """The repo's top and the pathspecs for this project; None if no diff may be taken.

    No diff when the repo's top is the home directory or above it (a
    dotfiles repo), or when the project isn't in this repo at all.
    """
    root = repo_root(cwd, deadline)
    if root is None:
        return None
    if area is None:
        return root, _scope()
    try:
        top, home = root.resolve(strict=True), area.home.resolve()
        project = area.project_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if top == home or top in home.parents:
        return None
    if project == top or project in top.parents:
        return root, _scope()
    if top in project.parents:
        return root, _scope(project.relative_to(top).as_posix())
    return None


def snapshot(cwd: Path, data_dir: Path, deadline: Deadline, area: Area | None = None) -> str | None:
    """Record the work tree (minus credential files) as a tree; its sha, or None.

    The real index is only copied, never written. The private index is a
    fresh file in ``data_dir`` each time, deleted afterwards with any lock
    git left on it: a git killed mid-``add`` can't block the next snapshot.
    """
    index_file: Path | None = None
    try:
        target = _target(cwd, deadline, area)
        if target is None:
            return None
        root, scope = target
        if _repo_defines_filters(root, deadline.left(GIT_TIMEOUT_S)):
            return None
        real = run_git(
            ["rev-parse", "--git-path", "index"], root, timeout=deadline.left(GIT_TIMEOUT_S)
        )
        if real is None:
            return None
        real_index = root / real.text.strip()
        data_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=data_dir, prefix="idx-")
        os.close(fd)
        index_file = Path(name)
        if real_index.is_file():
            # copy2 keeps the index's mtime, which git's racy-clean check
            # compares file mtimes with; a newer copy would hide some edits.
            shutil.copy2(real_index, index_file)
        else:
            index_file.unlink()  # git wants no index file rather than an empty one
        added = run_git(
            ["add", "-A", *scope],
            root,
            timeout=deadline.left(GIT_TIMEOUT_S),
            index_file=index_file,
        )
        if added is None:
            return None
        tree = run_git(
            ["write-tree"], root, timeout=deadline.left(GIT_TIMEOUT_S), index_file=index_file
        )
        return tree.text.strip() if tree and tree.text.strip() else None
    except OSError:
        return None
    finally:
        if index_file is not None:
            for leftover in (index_file, Path(f"{index_file}.lock")):
                with contextlib.suppress(OSError):
                    leftover.unlink(missing_ok=True)


def cap_diff(text: str, truncated: bool = False) -> str:
    """Keep a diff within the server's limit, saying so when it was cut."""
    if not truncated and len(text) <= MAX_DIFF_CHARS:
        return text
    return text[: MAX_DIFF_CHARS - len(TRUNCATED)] + TRUNCATED


def _diff(args: list[str], root: Path, deadline: Deadline, scope: list[str]) -> str | None:
    out = run_git(
        [*_DIFF_FLAGS, *args, *scope],
        root,
        timeout=deadline.left(GIT_TIMEOUT_S),
        limit=_DIFF_BYTES,
    )
    return None if out is None else cap_diff(out.text, out.truncated)


def diff_trees(
    cwd: Path, before: str, after: str, deadline: Deadline, area: Area | None = None
) -> str | None:
    """The diff between two snapshots, credential files excluded."""
    target = _target(cwd, deadline, area)
    return None if target is None else _diff([before, after], target[0], deadline, target[1])


def collect_diff(
    cwd: Path, deadline: Deadline | None = None, area: Area | None = None
) -> str | None:
    """The fallback diff when there is no snapshot from the start of the turn.

    ``git diff HEAD``; in a repo with no commits yet, the staged diff with a
    list of untracked files (credential files left out) in front of it.
    None when ``cwd`` isn't in a repo or git is missing. Only the project's
    subtree (see ``Area``).
    """
    deadline = deadline or Deadline(_NO_DEADLINE_S)
    target = _target(cwd, deadline, area)
    if target is None:
        return None
    root, scope = target
    if _repo_defines_filters(root, deadline.left(GIT_TIMEOUT_S)):
        return None
    head = run_git(
        ["rev-parse", "--verify", "-q", "HEAD"], root, timeout=deadline.left(GIT_TIMEOUT_S)
    )
    if head is not None:
        return _diff(["HEAD"], root, deadline, scope)
    staged = _diff(["--cached"], root, deadline, scope)
    listed = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z", *scope],
        root,
        timeout=deadline.left(GIT_TIMEOUT_S),
    )
    untracked = [
        p for p in (listed.text.split("\0") if listed else []) if p and not is_forbidden_path(p)
    ]
    if staged is None:
        return None
    if not untracked:
        return staged
    header = "Untracked files:\n" + "".join(f"  {p}\n" for p in untracked)
    return cap_diff(header + staged)
