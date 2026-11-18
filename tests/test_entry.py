"""The entry point Claude Code runs: ``python3 coach_hook.py <mode>``."""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

import coach_hook
from conftest import StubServer, prompt_payload

HOOK = Path(__file__).resolve().parents[1] / "plugins" / "promptcoach" / "hooks" / "coach_hook.py"


def test_runs_as_a_script(server: StubServer, env: dict[str, str], project: Path) -> None:
    """The real thing: a child process with only the standard library."""
    server.reply = {"message": "Hi.", "block_prompt": False, "prompt_only": False}
    child_env = {"PATH": os.environ["PATH"], **env}

    done = subprocess.run(
        [sys.executable, "-I", str(HOOK), "before"],
        input=json.dumps(prompt_payload(project)),
        capture_output=True,
        text=True,
        env=child_env,
        timeout=10,
        check=False,
    )

    assert done.returncode == 0
    assert json.loads(done.stdout) == {"systemMessage": "Hi."}
    assert done.stderr == ""


def test_main_guard(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], project: Path) -> None:
    monkeypatch.setattr(sys, "argv", [str(HOOK), "record"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(HOOK.parent)])
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(HOOK), run_name="__main__")

    assert exit_info.value.code == 0


def test_old_python_warns_once_per_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 8, 10, "final", 0))
    env = {"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}

    def run(mode: str, session: str) -> str:
        out = io.StringIO()
        stdin = io.StringIO(json.dumps({"session_id": session}))
        assert coach_hook.run([mode], stdin, out, env) == 0
        return out.getvalue()

    first = run("before", "s1")
    assert (
        "needs Python 3.9 or newer, and python3 here is 3.8" in json.loads(first)["systemMessage"]
    )
    assert run("before", "s1") == ""
    assert run("after", "s2") == ""
    assert run("before", "s2") != ""


def test_old_python_never_breaks_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 7, 0, "final", 0))
    out = io.StringIO()

    assert (
        coach_hook.run(
            ["before"], io.StringIO("not json"), out, {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        )
        == 0
    )
    assert coach_hook.run(["before"], io.StringIO("{}"), out, {}) == 0
    assert out.getvalue() == ""


def test_run_hands_over_to_the_client(env: dict[str, str], project: Path) -> None:
    out = io.StringIO()
    assert coach_hook.run(["record"], io.StringIO("{}"), out, env) == 0
    assert out.getvalue() == ""
