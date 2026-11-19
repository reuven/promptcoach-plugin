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
import subprocess
import sys
import threading
import time
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
    behind; conftest does it too, after a test that failed first."""
    gitdiff._RESUME.set()
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
