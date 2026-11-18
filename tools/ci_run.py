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
``TIMED_OUT`` (124, as ``timeout(1)``) at the deadline, ``TOO_MANY`` (125)
or ``TOO_MUCH_MEMORY`` (126) when its group passes ``MAX_PROCESSES`` or
``MAX_RSS_MB`` (a run that multiplies is stopped
while the machine still answers), or ``NOT_STARTED`` (127) if it couldn't
start. The group's process count is logged whenever it changes. The
step's stdout belongs to this process alone:
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
TOO_MANY = 125
TOO_MUCH_MEMORY = 126
NOT_STARTED = 127
POLL_S = 5.0  # how often the group's processes are counted
MAX_PROCESSES = 500  # more than this in the group: something is multiplying
MAX_RSS_MB = 3072  # more than this resident: something keeps allocating
RSS_STEP_MB = 100  # log the group's RSS when it moves by more than this
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


def _rows(pgid: int, ps_output: str) -> list[list[int]]:
    """The ``ps -A -o pid= -o pgid= [-o rss=]`` lines of group ``pgid``."""
    rows = []
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and all(f.isdigit() for f in fields) and int(fields[1]) == pgid:
            rows.append([int(f) for f in fields])
    return rows


def group_size(pgid: int, ps_output: str) -> int:
    """How many processes ``ps`` lists in group ``pgid``."""
    return len(_rows(pgid, ps_output))


def group_rss_kb(pgid: int, ps_output: str) -> int:
    """The resident memory of group ``pgid`` in KB (``ps``'s ``rss`` column)."""
    return sum(row[2] for row in _rows(pgid, ps_output) if len(row) >= 3)


def group_usage(pgid: int, windows: bool) -> tuple[int, int] | None:
    """(process count, RSS in KB) of group ``pgid``; None where it can't be
    told (Windows has no process groups ``ps`` can list). Never raises."""
    if windows:
        return None
    try:
        done = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "rss="],  # noqa: S607
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return group_size(pgid, done.stdout), group_rss_kb(pgid, done.stdout)


def count_group(pgid: int, windows: bool) -> int | None:
    """The process count of group ``pgid``, or None (see ``group_usage``)."""
    usage = group_usage(pgid, windows)
    return None if usage is None else usage[0]


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


def run(
    command: list[str],
    output: Path,
    deadline_s: float,
    echo: TextIO | None = None,
    poll_s: float = POLL_S,
    max_processes: int = MAX_PROCESSES,
    max_rss_mb: int = MAX_RSS_MB,
) -> int:
    """Run ``command`` until it ends, ``deadline_s`` passes, or its group
    holds more than ``max_processes`` processes or ``max_rss_mb`` MB; its
    status. Every ``poll_s`` the group is measured: the count is logged when
    it changes, the RSS when it moves by more than ``RSS_STEP_MB``, and the
    peak RSS at the end.
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
        last_count: int | None = None
        last_rss_mb = 0
        peak = [0]  # MB, kept across the loop and read by _stop
        while True:
            left = deadline_s - (time.monotonic() - started)
            try:
                code = proc.wait(max(min(poll_s, left), 0.01))
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - started >= deadline_s:
                note = (
                    f"\nci_run: timed out after {deadline_s:g} s "
                    f"({time.monotonic() - started:.0f} s); killed the process tree\n"
                )
                return _stop(proc, windows, reader, log, echo, note, TIMED_OUT, peak)
            usage = group_usage(proc.pid, windows)
            if usage is None:
                continue
            count, rss_mb = usage[0], usage[1] // 1024
            peak[0] = max(peak[0], rss_mb)
            if count != last_count:
                last_count = count
                _note(log, echo, f"ci_run: {count} processes in the group\n")
            if abs(rss_mb - last_rss_mb) > RSS_STEP_MB:
                last_rss_mb = rss_mb
                _note(log, echo, f"ci_run: group RSS {rss_mb} MB\n")
            if count > max_processes:
                note = f"\nci_run: more than {max_processes} processes in the group; killed it\n"
                return _stop(proc, windows, reader, log, echo, note, TOO_MANY, peak)
            if rss_mb > max_rss_mb:
                note = (
                    f"\nci_run: group RSS {rss_mb} MB passed the {max_rss_mb} MB cap; killed it\n"
                )
                return _stop(proc, windows, reader, log, echo, note, TOO_MUCH_MEMORY, peak)
        reader.join(_READER_GRACE_S)
        if not windows:
            _note(log, echo, f"ci_run: peak group RSS {peak[0]} MB\n")
    return code if code >= 0 else 128 - code


def _note(log: BinaryIO, echo: TextIO, text: str) -> None:
    log.write(text.encode())
    log.flush()
    echo.write(text)
    echo.flush()


def _stop(
    proc: subprocess.Popen[bytes],
    windows: bool,
    reader: threading.Thread,
    log: BinaryIO,
    echo: TextIO,
    note: str,
    code: int,
    peak: list[int],
) -> int:
    """Kill the command's tree, let the last output arrive, say why."""
    kill_tree(proc.pid, windows)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(_READER_GRACE_S)
    reader.join(_READER_GRACE_S)
    _note(log, echo, note)
    if not windows:
        _note(log, echo, f"ci_run: peak group RSS {peak[0]} MB\n")
    return code


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
