"""tools/ci_run.py: CI's pytest step, with a deadline nothing in the run can ignore."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import ci_run
import pytest

# A child that won't stop when asked: SIGTERM and SIGINT ignored (and SIGTERM
# blocked, where it can be), plus a grandchild in its group, then a long sleep.
DEAF_CHILD = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if hasattr(signal, "pthread_sigmask"):
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(grandchild.pid))
print("deaf and running", flush=True)
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    if os.name == "nt":  # pragma: no cover - Windows
        done = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        )
        return str(pid) in done.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_a_normal_exit_passes_its_status_and_output_through(tmp_path: Path) -> None:
    output, echo = tmp_path / "out.log", io.StringIO()
    script = "import sys; print('hello'); print('oops', file=sys.stderr); sys.exit(3)"

    code = ci_run.run([sys.executable, "-c", script], output, deadline_s=30, echo=echo)

    assert code == 3
    text = output.read_text()
    assert "hello" in text and "oops" in text
    assert "hello" in echo.getvalue()
    assert "timed out" not in text


def test_success_is_0(tmp_path: Path) -> None:
    code = ci_run.run([sys.executable, "-c", "pass"], tmp_path / "o", 30, echo=io.StringIO())
    assert code == 0


def test_a_child_that_ignores_sigterm_is_killed_at_the_deadline(tmp_path: Path) -> None:
    output, pid_file = tmp_path / "out.log", tmp_path / "grandchild.pid"
    started = time.monotonic()

    code = ci_run.run(
        [sys.executable, "-c", DEAF_CHILD, str(pid_file)], output, deadline_s=3, echo=io.StringIO()
    )

    assert code == ci_run.TIMED_OUT
    assert time.monotonic() - started < 15
    text = output.read_text()
    assert "deaf and running" in text
    assert "ci_run: timed out after 3 s" in text
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals; Windows has no SIGKILL")
def test_a_child_killed_by_a_signal_is_128_plus_it(tmp_path: Path) -> None:
    script = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    code = ci_run.run([sys.executable, "-c", script], tmp_path / "o", 30, echo=io.StringIO())
    assert code == 128 + signal.SIGKILL


def test_a_missing_command_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    output = tmp_path / "o"
    code = ci_run.run([str(tmp_path / "no-such-program")], output, 30, echo=io.StringIO())
    assert code == ci_run.NOT_STARTED
    assert "ci_run: could not start" in output.read_text()


def test_windows_kills_the_tree_with_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))

    ci_run.kill_tree(4321, windows=True)

    assert ran == [["taskkill", "/T", "/F", "/PID", "4321"]]


def test_posix_kills_the_whole_group(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    ci_run.kill_tree(4321, windows=False)

    assert killed == [(4321, 9)]


def test_kill_tree_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(*args: Any, **kwargs: Any) -> None:
        raise OSError("gone")

    monkeypatch.setattr(os, "killpg", gone, raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(subprocess, "run", gone)
    ci_run.kill_tree(1, windows=False)
    ci_run.kill_tree(1, windows=True)


def test_start_options_per_platform() -> None:
    assert ci_run.start_options(windows=False) == {"start_new_session": True}
    assert ci_run.start_options(windows=True) == {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    }


def test_main_parses_its_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "out.log"
    argv = ["--output", str(output), "--deadline", "30", "--"]
    argv += [sys.executable, "-c", "print('from main')"]

    assert ci_run.main(argv) == 0
    assert "from main" in output.read_text()
    assert "from main" in capsys.readouterr().out


def test_main_needs_a_command(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        ci_run.main(["--output", str(tmp_path / "o"), "--deadline", "30", "--"])
