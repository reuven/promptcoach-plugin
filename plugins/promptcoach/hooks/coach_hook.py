#!/usr/bin/env python3
"""Prompt coach hook for Claude Code: ``coach_hook.py before|record|after``.

Claude Code runs this with the hook's JSON payload on stdin. The work is
done by ``promptcoach_client``; this file only checks the Python version
first, in syntax old interpreters can still parse (string annotations, no
newer syntax), since the ``python3`` on a Mac may be too old for the client.
"""

import json
import os
import sys
import time
from typing import Mapping, TextIO  # noqa: UP035 - must import on old Pythons

STARTED = time.monotonic()  # the before hook's budget counts from here
MIN_PYTHON = (3, 9)
OLD_PYTHON_MESSAGE = (
    "Prompt coach needs Python 3.9 or newer, and python3 here is {}.{}. "
    "Install a newer Python (python.org, Homebrew or uv) to get coaching."
)


def _warn_old_python(
    argv: "list[str]", stdin: TextIO, stdout: TextIO, env: "Mapping[str, str]"
) -> int:
    """Say once per session that coaching needs a newer Python, then stay quiet."""
    data_dir = env.get("CLAUDE_PLUGIN_DATA")
    if argv[:1] != ["before"] or not data_dir:
        return 0
    try:
        session = str(json.loads(stdin.read()).get("session_id"))
        marker = os.path.join(data_dir, "old-python-warned")
        if os.path.exists(marker):
            with open(marker) as f:
                if f.read() == session:
                    return 0
        os.makedirs(data_dir, exist_ok=True)
        with open(marker, "w") as f:
            f.write(session)
        message = OLD_PYTHON_MESSAGE.format(*sys.version_info[:2])
        stdout.write(json.dumps({"systemMessage": message}))
    except Exception:  # noqa: BLE001, S110 - never break the session
        pass
    return 0


def utf8(stream: TextIO, encoding: str = "utf-8") -> None:
    """Make ``stream`` read or write UTF-8 (``utf-8-sig`` for stdin, which
    drops a byte order mark), whatever the locale.

    Why: Claude Code sends and reads UTF-8, but Python 3.9 to 3.14 use the
    locale's code page for stdio on Windows (often cp1252). Call it before
    anything is read. A stream that can't be reconfigured is left alone.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding=encoding, errors="replace")


def run(argv: "list[str]", stdin: TextIO, stdout: TextIO, env: "Mapping[str, str]") -> int:
    """Hand the hook to the client, or warn about an old Python."""
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        return _warn_old_python(argv, stdin, stdout, env)
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from promptcoach_client.claude_code import main
    except Exception:  # noqa: BLE001 - a half-updated install: silence, never exit 1
        return 0
    return main(argv, stdin, stdout, env, STARTED)


if __name__ == "__main__":
    utf8(sys.stdin, "utf-8-sig")
    utf8(sys.stdout)
    sys.exit(run(sys.argv[1:], sys.stdin, sys.stdout, os.environ))
