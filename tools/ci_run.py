"""Run CI's test command with a deadline nothing in the run can ignore.

Why this exists: on CI, a test run on Python 3.9 hung past GitHub's step
timeout and the job's, past the in-process faulthandler watchdog, and past
GitHub's own cancel, so the job ended with no log at all. A deadline kept
from outside the run doesn't depend on anything inside it: at the deadline
the command's whole process group gets SIGKILL (``taskkill /T /F`` on
Windows), which no ignored or blocked signal survives.

Usage: ``python tools/ci_run.py --output FILE --deadline SECONDS -- CMD...``

The command's output (stderr merged into stdout) is echoed and written to
``FILE`` line by line, so the file holds everything up to the moment of a
kill. The exit status is the command's own (128 + N if signal N killed it),
``TIMED_OUT`` (124, as ``timeout(1)``) at the deadline, or ``NOT_STARTED``
(127) if it couldn't start. The step's stdout belongs to this process alone:
a process the command leaves behind can hold the command's pipe open, never
the step's, so it can't keep the step running either. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, TextIO

TIMED_OUT = 124
NOT_STARTED = 127
_READER_GRACE_S = 5.0  # after a kill, how long to wait for the last output


def start_options(windows: bool) -> dict[str, Any]:
    """A process group of the command's own, so the kill reaches all of it."""
    if windows:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def kill_tree(pid: int, windows: bool) -> None:
    """Kill ``pid`` and everything it started. Never raises."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if windows:
            subprocess.run(  # noqa: S603 - a fixed system tool and a pid
                ["taskkill", "/T", "/F", "/PID", str(pid)],  # noqa: S607
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        else:
            os.killpg(pid, signal.SIGKILL)


def _copy(source: BinaryIO, log: BinaryIO, echo: TextIO) -> None:
    """Copy the command's output to the log and the echo, a line at a time."""
    try:
        with contextlib.suppress(Exception):  # a thread's error would print a traceback
            for line in iter(source.readline, b""):
                log.write(line)
                log.flush()
                echo.write(line.decode("utf-8", errors="replace"))
                echo.flush()
    finally:
        source.close()  # here, once the output ends: never under a blocked read


def run(command: list[str], output: Path, deadline_s: float, echo: TextIO | None = None) -> int:
    """Run ``command`` until it ends or ``deadline_s`` passes; its status.
    ``echo`` defaults to the stdout of the moment, not of import time."""
    echo = sys.stdout if echo is None else echo
    windows = os.name == "nt"
    with output.open("wb") as log:
        try:
            proc = subprocess.Popen(  # noqa: S603 - the command CI gives
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **start_options(windows),
            )
        except OSError as error:
            log.write(f"ci_run: could not start {command[0]}: {error}\n".encode())
            return NOT_STARTED
        assert proc.stdout is not None  # noqa: S101 - PIPE always sets it
        reader = threading.Thread(target=_copy, args=(proc.stdout, log, echo), daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            code = proc.wait(deadline_s)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid, windows)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(_READER_GRACE_S)
            reader.join(_READER_GRACE_S)
            note = (
                f"\nci_run: timed out after {deadline_s:g} s "
                f"({time.monotonic() - started:.0f} s); killed the process tree\n"
            )
            log.write(note.encode())
            echo.write(note)
            return TIMED_OUT
        reader.join(_READER_GRACE_S)
    return code if code >= 0 else 128 - code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")
    return run(command, args.output, args.deadline)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
