"""Fix round 1: time budgets, a read-only data dir, worker-thread errors,
blocking only for coach commands, safe messages, bounded stdin, redirects,
and ``python3 -I`` (I4, M2, M3, M5, M6, M7 and the mutation survivors).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    COMMAND_PROMPTS,
    ORDINARY_PROMPTS,
    TOKEN,
    StubServer,
    child_env,
    posix_only,
    prompt_payload,
    run_child_pytest,
    run_hook,
    stop_payload,
)
from promptcoach_client import claude_code, core, gitdiff
from promptcoach_client.claude_code import PROFILE, main
from promptcoach_client.contract import COMMAND_PATTERN, COMMAND_SYNONYMS
from server_contract import contract

ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = ROOT / "plugins" / "promptcoach" / "hooks" / "hooks.json"
BLOCK = {"message": "Coach: quieter now.", "block_prompt": True, "prompt_only": False}


def _not_prompt_only(data_dir: Path) -> None:
    data_dir.mkdir(exist_ok=True)
    (data_dir / "state.json").write_text('{"prompt_only": false}')


def _slow_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Put a git that sleeps before every call first on PATH (the reviewer's p4)."""
    real = shutil.which("git")
    assert real is not None
    slow = tmp_path / "slowgit"
    slow.mkdir()
    (slow / "git").write_text(f'#!/bin/sh\nsleep {seconds}\nexec {real} "$@"\n')
    (slow / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{slow}{os.pathsep}{os.environ['PATH']}")


# --- I4 / M5: one deadline per hook ------------------------------------------------------


@posix_only("test_windows.py::test_windows_git_that_hangs_is_killed_in_time", "a git written in sh")
def test_after_fits_its_budget_with_slow_git_and_a_slow_server(
    server: StubServer,
    env: dict[str, str],
    repo: Path,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe p4: git takes 2.4 s a call, the server 11.5 s; the hook gets 15 s."""
    _not_prompt_only(data_dir)
    (repo / "app.py").write_text("print('v2')\n")
    _slow_git(tmp_path, monkeypatch, 2.4)
    server.mode, server.delay = "sleep", 11.5

    started = time.monotonic()
    assert run_hook("after", stop_payload(repo), env) == ""
    assert time.monotonic() - started < core.AFTER_BUDGET_S + 0.5
    assert core.AFTER_BUDGET_S <= 13.5


@posix_only("test_windows.py::test_windows_git_that_hangs_is_killed_in_time", "a git written in sh")
def test_before_fits_its_budget_with_slow_git_and_a_slow_server(
    server: StubServer,
    env: dict[str, str],
    repo: Path,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _not_prompt_only(data_dir)
    _slow_git(tmp_path, monkeypatch, 1.0)
    server.mode = "sleep"

    started = time.monotonic()
    assert run_hook("before", prompt_payload(repo), env) == ""
    assert time.monotonic() - started < core.BEFORE_BUDGET_S + 0.3
    assert core.BEFORE_BUDGET_S <= 2.6


def test_spent_budget_skips_the_remaining_steps(
    server: StubServer,
    env: dict[str, str],
    repo: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is started once the budget is gone: no git call, no request."""
    _not_prompt_only(data_dir)
    monkeypatch.setattr(core, "AFTER_BUDGET_S", 0.0)
    calls: list[list[str]] = []
    monkeypatch.setattr(gitdiff, "run_git", lambda args, cwd, **kw: calls.append(args))

    assert run_hook("after", stop_payload(repo), env) == ""
    assert calls == []
    assert server.requests == []


def test_deadline() -> None:
    deadline = gitdiff.Deadline(10.0)
    assert 9.0 < deadline.left() <= 10.0
    assert deadline.left(cap=3.0) == 3.0
    assert gitdiff.Deadline(-1.0).left() == 0.0
    assert gitdiff.Deadline(-1.0).spent()
    assert not deadline.spent()
    assert gitdiff.run_git(["status"], ROOT, timeout=0.0) is None


# --- M3: a read-only data dir never loses the reply ---------------------------------------


@posix_only("test_windows.py::test_unwritable_data_dir_still_blocks", "chmod 0o555")
def test_read_only_data_dir_still_blocks(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    _not_prompt_only(data_dir)
    data_dir.chmod(0o555)
    server.reply = BLOCK
    try:
        out = run_hook("before", prompt_payload(project, prompt="coach, quieter"), env)
        after = run_hook("after", stop_payload(project), env)
    finally:
        data_dir.chmod(0o755)

    assert json.loads(out) == {"decision": "block", "reason": "Coach: quieter now."}
    assert json.loads(after) == {"systemMessage": "Coach: quieter now."}


# --- M6: worker threads never print -------------------------------------------------------


def test_bad_status_line_leaves_stderr_empty(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.mode = "badstatus"
    hook = ROOT / "plugins" / "promptcoach" / "hooks" / "coach_hook.py"

    done = subprocess.run(
        [sys.executable, "-I", str(hook), "before"],
        input=json.dumps(prompt_payload(project)),
        capture_output=True,
        text=True,
        env=child_env(env),
        timeout=10,
        check=False,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_snapshot_thread_errors_are_swallowed(
    server: StubServer,
    env: dict[str, str],
    repo: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("boom")

    _not_prompt_only(data_dir)
    monkeypatch.setattr(gitdiff, "snapshot", broken)
    server.reply = {"message": "Hi.", "block_prompt": False, "prompt_only": False}

    assert json.loads(run_hook("before", prompt_payload(repo), env)) == {"systemMessage": "Hi."}


# --- M7: block only a coach command; safe, short messages ----------------------------------


@pytest.mark.parametrize("prompt", COMMAND_PROMPTS)
def test_block_is_honored_for_a_coach_command(
    prompt: str, server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = BLOCK
    out = json.loads(run_hook("before", prompt_payload(project, prompt=prompt), env))
    assert out == {"decision": "block", "reason": "Coach: quieter now."}


@pytest.mark.parametrize("prompt", ORDINARY_PROMPTS)
def test_block_is_ignored_for_an_ordinary_prompt(
    prompt: str, server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = BLOCK
    out = json.loads(run_hook("before", prompt_payload(project, prompt=prompt), env))
    assert out == {"systemMessage": "Coach: quieter now."}


def test_command_pattern_matches_the_server() -> None:
    assert contract()["command_pattern"] == COMMAND_PATTERN
    assert contract()["command_flags"] == ["ASCII", "IGNORECASE"]
    assert sorted(COMMAND_SYNONYMS) == contract()["command_synonyms"]


def test_messages_lose_control_characters_and_are_capped(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = {
        "message": "\x1b]8;;https://evil.example\x07click\x1b]8;;\x07 \x1b[2J\x1b[31mRED"
        "\u009b31m\x00 line\n\ttab\x7f" + "x" * 10_000,
        "block_prompt": False,
        "prompt_only": False,
    }

    message = json.loads(run_hook("before", prompt_payload(project), env))["systemMessage"]

    assert message.startswith("]8;;https://evil.exampleclick]8;; [2J[31mRED31m line\n\ttab")
    assert not any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 for c in message)
    assert len(message.encode()) <= 4096


def test_block_reason_is_cleaned_too() -> None:
    out = io.StringIO()
    claude_code.emit(core.Reply("a\x1b[2Jb", True, False), out, allow_block=True)
    assert json.loads(out.getvalue()) == {"decision": "block", "reason": "a[2Jb"}


# --- mutation survivors: Stop never blocks, stdin is bounded, no redirect -----------------


def test_after_never_returns_a_block(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = BLOCK
    event = core.TurnEnded("sess-1", "prompt-1", project, project, "done", continuing=False)
    config = claude_code.config_from_env(env)

    reply = core.after(event, config, PROFILE)

    assert reply == core.Reply("Coach: quieter now.", False, False)
    assert json.loads(run_hook("after", stop_payload(project), env)) == {
        "systemMessage": "Coach: quieter now."
    }


def test_stdin_read_is_bounded(
    server: StubServer, env: dict[str, str], project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.dumps(prompt_payload(project))
    monkeypatch.setattr(claude_code, "MAX_STDIN_CHARS", len(payload) - 1)

    assert main(["before"], io.StringIO(payload), io.StringIO(), env) == 0
    assert server.requests == []  # the cut-off JSON never parsed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_to_another_port_never_gets_the_token(
    status: int,
    server: StubServer,
    other_server: StubServer,
    env: dict[str, str],
    project: Path,
) -> None:
    server.mode, server.redirect_status = "redirect", status
    server.redirect_to = other_server.url + "/v1/before"
    other_server.reply = {"message": "Hi.", "block_prompt": False, "prompt_only": False}

    assert run_hook("before", prompt_payload(project), env) == ""
    assert len(server.requests) == 1
    assert other_server.requests == []
    assert TOKEN not in json.dumps([r.headers for r in other_server.requests])


# --- M2: python -I lives in test_hook_command.py ------------------------------------------


def test_adapter_never_blocks_at_the_end_of_a_turn(
    server: StubServer, env: dict[str, str], project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the core returned a block, Stop output is only a message."""
    monkeypatch.setattr(core, "after", lambda *args: core.Reply("x", True, False))
    assert json.loads(run_hook("after", stop_payload(project), env)) == {"systemMessage": "x"}


# --- a test that hangs in a blocking call ends the run, naming itself ---------------------

STUCK_TEST = """
import time

def test_stuck_in_a_blocking_call():
    time.sleep(600)
"""


# A test that passes but leaves a thread nobody stops: Python waits for it at
# exit, after every test, where a per-test watchdog no longer looks.
STUCK_AT_EXIT = """
import threading

def test_passes_but_leaves_a_thread_behind():
    threading.Thread(target=threading.Event().wait, daemon=False).start()
"""


def test_a_hang_after_the_last_test_dumps_every_stack_and_ends_the_run(tmp_path: Path) -> None:
    """The watchdog stays armed from start to exit, not just inside tests."""
    child = run_child_pytest(tmp_path, "test_exit.py", STUCK_AT_EXIT)

    assert child.code != 0
    assert child.seconds < 30
    assert "after test_exit.py::test_passes_but_leaves_a_thread_behind" in child.hang_log
    assert "exiting" in child.hang_log
    assert "Timeout" in child.hang_log and "wait" in child.hang_log.rsplit("exiting", 1)[-1]


def test_a_run_without_a_hang_exits_cleanly(tmp_path: Path) -> None:
    child = run_child_pytest(tmp_path, "test_fine.py", "def test_fine():\n    pass\n")

    assert child.code == 0
    assert "running test_fine.py::test_fine" in child.hang_log
    assert "Timeout" not in child.hang_log


def test_a_hung_test_dumps_its_stack_and_ends_the_run(tmp_path: Path) -> None:
    """The per-test timer can't interrupt a blocking call on Windows; the
    faulthandler backstop can: past the hard limit it writes every thread's
    stack to the hang log and exits non-zero, long before CI's job timeout."""
    child = run_child_pytest(tmp_path, "test_stuck.py", STUCK_TEST)

    assert child.code != 0
    assert child.seconds < 30
    assert "running test_stuck.py::test_stuck_in_a_blocking_call" in child.hang_log
    after = child.hang_log.split("running", 2)[-1]
    assert "Timeout" in child.hang_log and "test_stuck_in_a_blocking_call" in after


# --- a child pytest is hermetic, and can't start another ------------------------------

# What the child sees: its depth, its config, and how many tests it collected.
INTROSPECT = """
import os, sys

def test_where_am_i(request):
    config = request.config
    print("DEPTH", os.environ.get("PROMPTCOACH_CHILD_DEPTH"))
    print("INI", config.inipath)
    print("ROOT", config.rootpath)
    print("ADDOPTS", config.getini("addopts"))
    print("ENV", sorted(k for k in ("CI", "COVERAGE_FILE", "PYTEST_ADDOPTS", "COV_CORE_SOURCE")
                        if k in os.environ))
    print("ITEMS", len(request.session.items))
"""


def test_a_child_pytest_runs_only_its_own_file_with_its_own_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever CI's environment holds (CI, PYTEST_ADDOPTS, COVERAGE_FILE),
    the child runs one file, with an empty config rooted in its own temp dir:
    never the repo's addopts or testpaths, so never the whole suite again."""
    for key, value in {"CI": "true", "PYTEST_ADDOPTS": "tests", "COVERAGE_FILE": "x"}.items():
        monkeypatch.setenv(key, value)
    child = run_child_pytest(tmp_path, "test_where.py", INTROSPECT, extra_args=["-s"])

    assert child.code == 0, child.output
    lines = dict(
        line.split(" ", 1)
        for line in child.output.splitlines()
        if line[:3].isupper()
        and " " in line
        and line.split(" ", 1)[0] in {"DEPTH", "INI", "ROOT", "ADDOPTS", "ENV", "ITEMS"}
    )
    assert lines["DEPTH"] == "1"
    assert Path(lines["INI"]) == tmp_path / "pytest.ini"
    assert Path(lines["ROOT"]) == tmp_path
    assert lines["ADDOPTS"] == "[]"
    assert lines["ENV"] == "[]"
    assert lines["ITEMS"] == "1"


def test_a_child_pytest_never_starts_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a child (PROMPTCOACH_CHILD_DEPTH set), starting another fails
    loudly instead of multiplying runs."""
    monkeypatch.setenv("PROMPTCOACH_CHILD_DEPTH", "1")

    with pytest.raises(RuntimeError, match="already inside a child pytest"):
        run_child_pytest(tmp_path, "test_x.py", "def test_x():\n    pass\n")
    assert not (tmp_path / "test_x.py").exists()


def test_the_hard_limit_leaves_the_per_test_timer_to_act_first() -> None:
    import conftest

    assert conftest.HARD_LIMIT_S == conftest.TEST_TIMEOUT_S + 30


# --- nothing the suite starts outlives it -------------------------------------------------

LEAVES_A_PROCESS = """
import subprocess, sys

def test_starts_something_and_forgets_it():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
"""


@pytest.mark.skipif(os.name == "nt", reason="the survivor check reads ps (POSIX)")
def test_a_process_left_running_fails_the_run_and_is_named(tmp_path: Path) -> None:
    """A process that outlives the suite can hold a pipe open (CI's step
    output among them), so the session's end lists it, kills it, and fails."""
    child = run_child_pytest(tmp_path, "test_leak.py", LEAVES_A_PROCESS)

    assert child.code != 0, child.output
    assert "still running after the suite" in child.output
    assert "time.sleep(60)" in child.output
    [pid] = re.findall(r"^\s*(\d+) .*time\.sleep\(60\)", child.output, re.M)
    with pytest.raises(OSError):
        os.kill(int(pid), 0)  # the check killed it


def test_survivors_are_found_by_pid_or_group() -> None:
    import conftest

    ps = "  10  10 S  python a\n  11  10 S  sleep 60\n  12  12 Z  (python)\n  13  99 S  other\n"
    found = conftest.survivors({10, 12}, ps, me=1)
    assert [row.split()[0] for row in found] == ["10", "11"]


def test_survivor_rows_are_never_cut_to_the_terminal_width() -> None:
    """procps ps cuts each row to 80 columns when its output is a pipe, which
    hid the command on Linux CI; -ww keeps the whole command line."""
    import conftest

    assert "-ww" in conftest.PS_ARGV
