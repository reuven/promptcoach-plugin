"""Fix round 1: time budgets, a read-only data dir, worker-thread errors,
blocking only for coach commands, safe messages, bounded stdin, redirects,
and ``python3 -I`` (I4, M2, M3, M5, M6, M7 and the mutation survivors).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import TOKEN, StubServer, prompt_payload, run_hook, stop_payload
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
        env={"PATH": os.environ["PATH"], **env},
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


@pytest.mark.parametrize(
    "prompt",
    [
        "coach, quieter",
        "Coach: status?",
        "coach be louder please",
        "  coach,   prompt-only on.  ",
        "coach, that wasn\u2019t helpful",
        "COACH learning off",
        "coach, thanks!",
    ],
)
def test_block_is_honored_for_a_coach_command(
    prompt: str, server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = BLOCK
    out = json.loads(run_hook("before", prompt_payload(project, prompt=prompt), env))
    assert out == {"decision": "block", "reason": "Coach: quieter now."}


@pytest.mark.parametrize(
    "prompt",
    [
        "add a test",
        "coach, helpful?",
        "coach, that helped. Now add tests",
        "coach, be helpful",
        "coach, quieter\nand then fix the bug",
        "coach, thank\u017f",
        "coach, quieter" + " " * 1_000,  # a command is short; long prompts aren't read
    ],
)
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


# --- M2: python3 -I, so the project's own modules never load -------------------------------


def test_hooks_run_python_isolated() -> None:
    hooks = json.loads(HOOKS_JSON.read_text())["hooks"]
    for groups in hooks.values():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["command"] == "python3"
                assert hook["args"][0] == "-I"


def test_project_json_py_never_runs(tmp_path: Path, data_dir: Path) -> None:
    project = tmp_path / "evil-project"
    project.mkdir()
    marker = tmp_path / "MARKER"
    (project / "json.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    hook = json.loads(HOOKS_JSON.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    args = [
        a.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT / "plugins" / "promptcoach"))
        for a in hook["args"]
    ]

    subprocess.run(
        [sys.executable, *args],
        input="{}",
        cwd=project,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": ".", "CLAUDE_PLUGIN_DATA": str(data_dir)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert not marker.exists()


def test_adapter_never_blocks_at_the_end_of_a_turn(
    server: StubServer, env: dict[str, str], project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the core returned a block, Stop output is only a message."""
    monkeypatch.setattr(core, "after", lambda *args: core.Reply("x", True, False))
    assert json.loads(run_hook("after", stop_payload(project), env)) == {"systemMessage": "x"}
