"""1.0.0b4 item 10: no git thread runs Python once the exit cleanup is over.

CI (macos-latest, Apple's Python 3.9.6) once saw a hook child die with
SIGSEGV at exit in test_round4's test_exit_while_another_thread_starts_git_kills_it.
The exit cleanup kills the running git, and that kill wakes the threads
waiting on it (run_git's, and its reader's) just as the interpreter
finalizes; CPython 3.9 can crash when a daemon thread takes the GIL during
finalization. The fix: the cleanup waits (within its 0.3 s) until each git
thread has parked, waiting forever on a lock with every signal blocked, so
none of them runs Python again; and a git that finishes starting after the
cleanup is killed at once.

The child tests force the interleaving: an atexit handler registered before
the cleanup (so it runs after it) holds the interpreter open for a while; a
thread that returns from run_git in that time is a thread that ran Python
past the cleanup. The in-process tests cover the same code on every OS.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path

import pytest
from test_round4 import HOOK, _fake_git

from conftest import child_env, posix_only
from promptcoach_client import gitdiff

# install_cleanup comes after the handler, so the handler runs after the cleanup.
PARK_HEAD = """
import atexit, os, pathlib, signal, subprocess, sys, threading, time
sys.path.insert(0, sys.argv[1])
from promptcoach_client import gitdiff
returned = threading.Event()
def after_cleanup():
    print("returned" if returned.wait(1.5) else "parked", flush=True)
atexit.register(after_cleanup)
gitdiff.install_cleanup()
def work():
    gitdiff.run_git(["status"], pathlib.Path(sys.argv[2]), timeout=30)
    returned.set()
"""

TRACKED = """
threading.Thread(target=work, daemon=True).start()
while not gitdiff._LIVE:
    time.sleep(0.01)
"""

STARTING = """
real = subprocess.Popen
started = threading.Event()
class SlowPopen(real):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        started.set()
        time.sleep(0.2)  # git runs, not yet in _LIVE; the cleanup waits for it
gitdiff.subprocess.Popen = SlowPopen
threading.Thread(target=work, daemon=True).start()
started.wait(10)
"""


@posix_only(
    "test_exit_park.py::test_exit_cleanup_returns_once_the_git_thread_is_parked",
    "a sh git found by pgrep",
)
@pytest.mark.parametrize("where", ["tracked", "starting"])
def test_no_git_thread_runs_python_after_the_exit_cleanup(tmp_path: Path, where: str) -> None:
    seconds = f"30.{os.getpid()}{time.monotonic_ns() % 10**6}"
    fake, pid_file = _fake_git(tmp_path, seconds)
    script = PARK_HEAD + (TRACKED if where == "tracked" else STARTING)
    child = subprocess.run(
        [sys.executable, "-I", "-c", script, str(HOOK.parent), str(tmp_path), str(pid_file)],
        env=child_env(path=f"{fake}{os.pathsep}{os.environ['PATH']}"),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    time.sleep(0.3)
    pattern = f"sleep {seconds}"
    left = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    subprocess.run(["pkill", "-9", "-f", pattern], check=False)
    assert (child.returncode, child.stdout.split(), child.stderr) == (0, ["parked"], "")
    assert left.stdout.split() == []


# --- In process, on every OS: a stand-in git that just waits -----------------------------

WAITING_GIT = "import sys, time; time.sleep(30)"


def _waiting_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitdiff, "GIT_COMMAND", (sys.executable, "-c", WAITING_GIT))


def _resume(thread: threading.Thread) -> None:
    """Let a parked thread go (no hook ever does), so the test leaves none
    behind; conftest does it too, after a test that failed first. A thread
    parked on _PARKED needs a notify to see _RESUME."""
    gitdiff._RESUME.set()
    with gitdiff._PARKED:
        gitdiff._PARKED.notify_all()
    thread.join(10)


def test_exit_cleanup_returns_once_the_git_thread_is_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waiting_git(monkeypatch)
    returned: list[object] = []
    thread = threading.Thread(
        target=lambda: returned.append(gitdiff.run_git(["status"], tmp_path, timeout=30)),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not gitdiff._LIVE:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    started = time.monotonic()
    gitdiff._exit_cleanup()
    took = time.monotonic() - started
    thread.join(0.5)  # killed git: before the fix, run_git returns within ms
    assert returned == []
    assert thread.is_alive()
    assert took < 0.3 + 0.2
    assert not gitdiff._LIVE
    _resume(thread)
    assert returned == [None]


def test_run_git_after_the_exit_cleanup_starts_no_git_and_parks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "git-ran"
    monkeypatch.setattr(
        gitdiff,
        "GIT_COMMAND",
        (sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),
    )
    gitdiff._exit_cleanup()
    returned: list[object] = []
    thread = threading.Thread(
        target=lambda: returned.append(gitdiff.run_git(["status"], tmp_path, timeout=30)),
        daemon=True,
    )
    thread.start()
    thread.join(0.5)
    assert returned == []
    assert thread.is_alive()
    _resume(thread)
    assert returned == [None]
    assert not marker.exists()


def test_a_start_that_ends_after_the_exit_cleanup_kills_its_git_and_parks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start stalls past the cleanup's wait (p32: it doesn't hold the
    exit); once git is started it is killed at once, not left to run."""
    _waiting_git(monkeypatch)
    monkeypatch.setattr(gitdiff, "_EXIT_WAIT_S", 0.05)
    current = subprocess.Popen
    procs: list[subprocess.Popen[bytes]] = []
    in_start, cleaned = threading.Event(), threading.Event()

    def slow_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        in_start.set()
        cleaned.wait(10)  # the start ends only after the cleanup is over
        proc = current(*args, **kwargs)  # type: ignore[call-overload]
        procs.append(proc)
        return proc  # type: ignore[no-any-return]

    monkeypatch.setattr(subprocess, "Popen", slow_popen)
    returned: list[object] = []
    thread = threading.Thread(
        target=lambda: returned.append(gitdiff.run_git(["status"], tmp_path, timeout=30)),
        daemon=True,
    )
    thread.start()
    try:
        assert in_start.wait(10)
        gitdiff._exit_cleanup()
        cleaned.set()
        deadline = time.monotonic() + 10
        while not procs:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert procs[0].wait(5) is not None  # killed, not left to run its 30 s
        thread.join(0.5)
        assert returned == []
        assert thread.is_alive()
    finally:
        cleaned.set()
        for proc in procs:
            proc.kill()
    _resume(thread)
    assert returned == [None]


@posix_only(
    "test_exit_park.py::test_exit_cleanup_returns_once_the_git_thread_is_parked",
    "signal masks are POSIX",
)
def test_a_git_thread_blocks_signals_before_its_count_drops_and_parks_in_one_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round M6: between the decrement that lets the exit cleanup return
    and the park, the thread must run no Python with signals open. So it
    blocks every signal while still counted and holding _BUSY_CHANGED, and
    parks by waiting on _PARKED, which shares that lock (and drops it and
    sleeps in one step), never after letting go of it."""
    gitdiff._EXITING.set()
    real = signal.pthread_sigmask
    seen: list[tuple[int, bool]] = []

    def sigmask(how: int, mask: Iterable[int]) -> set[int]:
        mask = set(mask)
        if how == signal.SIG_BLOCK and mask == set(signal.valid_signals()):
            owned = bool(gitdiff._BUSY_CHANGED._is_owned())  # type: ignore[attr-defined]
            seen.append((gitdiff._BUSY[0], owned))
        return set(real(how, mask))

    monkeypatch.setattr(signal, "pthread_sigmask", sigmask)

    def work() -> None:
        with gitdiff._git_thread():
            pass

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not seen:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    thread.join(0.3)
    assert thread.is_alive()  # parked
    # Still counted, and inside the critical section, when signals close.
    assert seen == [(1, True)]
    # The park released _BUSY_CHANGED: the exit cleanup can take it at once.
    assert gitdiff._BUSY_CHANGED.acquire(timeout=1)
    assert gitdiff._BUSY[0] == 0
    gitdiff._BUSY_CHANGED.release()
    _resume(thread)
    assert not thread.is_alive()


class _CountingEvent(threading.Event):
    """_RESUME's stand-in: counts, per thread, how often a parked thread
    checks it, which it does each time its wait wakes."""

    def __init__(self) -> None:
        super().__init__()
        self.checks: dict[str, int] = {}

    def is_set(self) -> bool:
        name = threading.current_thread().name
        self.checks[name] = self.checks.get(name, 0) + 1
        return super().is_set()


def _parked(checks: dict[str, int], name: str) -> None:
    """Wait until NAME has checked once and released the lock (it waits)."""
    deadline = time.monotonic() + 10
    while not checks.get(name):
        assert time.monotonic() < deadline, f"{name} never parked"
        time.sleep(0.01)
    assert gitdiff._BUSY_CHANGED.acquire(timeout=10)
    gitdiff._BUSY_CHANGED.release()


def test_a_parked_thread_is_not_woken_by_another_git_threads_decrement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 2, item 5: with a tracked git, run_git's thread and its
    reader are both counted. The reader parks first; run_git's thread then
    drops its count, and that decrement's notify must not wake the parked
    reader, which would then run Python while the interpreter finalizes.
    So a parked thread waits on a condition no decrement notifies."""
    resume = _CountingEvent()
    monkeypatch.setattr(gitdiff, "_RESUME", resume)
    # Windows has no pthread_sigmask (gitdiff never calls it there): set it
    # where it's missing too, so the test runs on every platform.
    monkeypatch.setattr(signal, "pthread_sigmask", lambda how, mask: set(), raising=False)
    gitdiff._EXITING.set()
    go = {"reader": threading.Event(), "run_git": threading.Event()}

    def work(name: str) -> None:
        with gitdiff._git_thread():
            go[name].wait(10)

    threads = [threading.Thread(target=work, args=(name,), name=name, daemon=True) for name in go]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10
    while gitdiff._BUSY[0] < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    go["reader"].set()
    _parked(resume.checks, "reader")
    go["run_git"].set()
    _parked(resume.checks, "run_git")
    # Before the fix the reader wakes on run_git's notify (a second check)
    # within milliseconds; give it far longer than that.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and resume.checks["reader"] == 1:
        time.sleep(0.01)
    assert resume.checks == {"reader": 1, "run_git": 1}
    assert gitdiff._BUSY[0] == 0
    for thread in threads:
        _resume(thread)
    assert not any(thread.is_alive() for thread in threads)


# --- Fix round 3: a git whose start ends after the cleanup gave up -------------------------
#
# On a slow runner (macos-latest, 3.14) the start outlasted the cleanup's 0.3 s:
# the cleanup returned, the process ended, and the starting thread never got
# to kill its git, which was left running. The thread can't be relied on
# after the cleanup returns (it may never run again), so the cleanup must be
# able to kill a git it hasn't been handed yet.

LATE_START = """
real = subprocess.Popen
started = threading.Event()
class LatePopen(real):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)  # git is running: forked, exec'd, its own session
        started.set()
        time.sleep(10)  # ... and the start returns long after the process is gone
        pathlib.Path(sys.argv[2], "ran-after-the-cleanup").touch()
gitdiff.subprocess.Popen = LatePopen
threading.Thread(target=work, daemon=True).start()
started.wait(10)
"""


@posix_only(
    "test_exit_park.py::test_the_cleanup_kills_a_git_whose_start_has_not_returned",
    "a sh git found by pgrep",
)
def test_a_git_whose_start_ends_after_the_process_is_gone_is_not_orphaned(
    tmp_path: Path,
) -> None:
    seconds = f"30.{os.getpid()}{time.monotonic_ns() % 10**6}"
    fake, pid_file = _fake_git(tmp_path, seconds)
    begun = time.monotonic()
    child = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            PARK_HEAD + LATE_START,
            str(HOOK.parent),
            str(tmp_path),
            str(pid_file),
        ],
        env=child_env(path=f"{fake}{os.pathsep}{os.environ['PATH']}"),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    took = time.monotonic() - begun
    time.sleep(0.3)
    pattern = f"sleep {seconds}"
    left = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    subprocess.run(["pkill", "-9", "-f", pattern], check=False)
    # The exit still doesn't wait for the stalled start (the merge review's
    # ruling, p32), the thread runs no Python past the cleanup, and no git
    # is left behind.
    assert (child.returncode, child.stdout.split(), child.stderr) == (0, ["parked"], "")
    # Well under LatePopen's 10 s, so even a slow runner's exit (the 1.5 s,
    # the cleanup's 0.3 s, interpreter start-up) ends before the late touch.
    assert took < 8
    assert not (tmp_path / "ran-after-the-cleanup").exists()
    assert left.stdout.split() == []


@posix_only(
    "test_exit_park.py::test_a_start_that_ends_after_the_exit_cleanup_kills_its_git_and_parks",
    "the started git reports its own pid before exec (preexec_fn)",
)
def test_the_cleanup_kills_a_git_whose_start_has_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forced interleaving, in process: git is running but Popen hasn't
    returned (so run_git hasn't put it in _LIVE) until after the cleanup is
    over. The cleanup must kill it anyway."""
    _waiting_git(monkeypatch)
    monkeypatch.setattr(gitdiff, "_EXIT_WAIT_S", 0.05)
    current = subprocess.Popen
    procs: list[subprocess.Popen[bytes]] = []
    running, cleaned = threading.Event(), threading.Event()

    def late_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = current(*args, **kwargs)  # type: ignore[call-overload]
        procs.append(proc)
        running.set()
        cleaned.wait(10)  # returns only once the cleanup is over
        return proc  # type: ignore[no-any-return]

    monkeypatch.setattr(subprocess, "Popen", late_popen)
    thread = threading.Thread(
        target=gitdiff.run_git, args=(["status"], tmp_path), kwargs={"timeout": 30}, daemon=True
    )
    thread.start()
    try:
        assert running.wait(10)
        gitdiff._exit_cleanup()
        # Killed by the cleanup itself, before the start returned.
        deadline = time.monotonic() + 5
        while _alive(procs[0].pid):
            assert time.monotonic() < deadline, "the cleanup left the git running"
            time.sleep(0.02)
    finally:
        cleaned.set()
        for proc in procs:
            proc.kill()
            proc.wait(5)
    _resume(thread)


def _alive(pid: int) -> bool:
    """True while ``pid`` runs; a zombie (killed, not yet reaped) is not running."""
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:  # pragma: no cover
        return False
    return bool(state) and not state.startswith("Z")


# The child's half of the handshake runs between fork and exec, where coverage
# can't see it: the same function, in process, posing as a child.


class _Exited(Exception):
    pass


def _as_child(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    codes: list[int] = []

    def exit_(code: int) -> None:
        codes.append(code)
        raise _Exited

    monkeypatch.setattr(gitdiff, "_PARENT_PID", -1)
    monkeypatch.setattr(os, "_exit", exit_)
    return codes


@posix_only("test_windows.py::test_start_options_per_platform", "preexec_fn is POSIX")
def test_a_git_child_reports_its_pid_and_runs_on_while_the_hook_is_not_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = _as_child(monkeypatch)
    gitdiff._in_git_child()
    assert codes == []
    assert gitdiff._unreturned_starts() == [os.getpid()]
    assert gitdiff._unreturned_starts() == []


@posix_only("test_windows.py::test_start_options_per_platform", "preexec_fn is POSIX")
def test_a_git_child_ends_before_exec_once_the_hook_is_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = _as_child(monkeypatch)
    os.write(gitdiff._EXIT_BEGUN_W, b"x")
    with pytest.raises(_Exited):
        gitdiff._in_git_child()
    assert codes == [126]
    gitdiff._unreturned_starts()


@posix_only("test_windows.py::test_start_options_per_platform", "preexec_fn is POSIX")
def test_a_git_child_runs_on_when_it_cannot_report_or_the_flag_pipe_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = _as_child(monkeypatch)
    read, write = os.pipe()
    os.close(write)  # nobody can write the flag: EOF, b""
    monkeypatch.setattr(gitdiff, "_STARTED_W", -1)  # the report fails: EBADF
    monkeypatch.setattr(gitdiff, "_EXIT_BEGUN_R", read)
    try:
        gitdiff._in_git_child()
        assert gitdiff._drain(read) == b""
    finally:
        os.close(read)
    assert codes == []
    assert gitdiff._unreturned_starts() == []


def test_drain_without_a_pipe_is_empty() -> None:
    assert gitdiff._drain(-1) == b""


def test_exit_cleanup_without_or_with_a_broken_flag_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no flag pipe; a write that fails must not stop the cleanup."""
    killed: list[int] = []
    monkeypatch.setattr(gitdiff, "_kill_live", lambda: killed.append(1))
    for fd in (-1, 2**20):
        monkeypatch.setattr(gitdiff, "_EXIT_BEGUN_W", fd)
        gitdiff._exit_cleanup()
    assert killed == [1, 1]
