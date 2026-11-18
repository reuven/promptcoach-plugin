"""Task 16: the tool-run recorder and the after-turn hook (``Stop`` → ``/v1/after``)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import (
    StubServer,
    bash_payload,
    failure_payload,
    git,
    prompt_payload,
    run_hook,
    stop_payload,
)
from promptcoach_client import gitdiff

FULL = {"message": None, "block_prompt": False, "prompt_only": False}


def _state(data_dir: Path, **state: object) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state.json").write_text(json.dumps(state))


def _runs(data_dir: Path) -> list[dict[str, object]]:
    path = data_dir / "runs-sess-1"
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- record ------------------------------------------------------------------------


def test_record_appends_run(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    assert run_hook("record", bash_payload(project, "uv run pytest -q"), env) == ""
    assert (
        run_hook(
            "record",
            bash_payload(project, "ls", tool_response={"stdout": "a\nb", "stderr": "warn"}),
            env,
        )
        == ""
    )

    assert _runs(data_dir) == [
        {
            "prompt_id": "prompt-1",
            "command": "uv run pytest -q",
            "exit_code": 0,
            "output_tail": "out",
            "is_test": True,
        },
        {
            "prompt_id": "prompt-1",
            "command": "ls",
            "exit_code": 0,
            "output_tail": "a\nb\nwarn",
            "is_test": False,
        },
    ]
    assert server.requests == []  # recording never touches the network


@pytest.mark.parametrize(
    ("error", "interrupt", "exit_code", "output"),
    [
        ("Exit code 2\nls: /nonexistent: No such file", False, 2, "ls: /nonexistent: No such file"),
        ("Exit code 1", False, 1, ""),
        ("Command timed out after 2m", False, None, "Command timed out after 2m"),
        ("Exit code 130\n^C", True, None, "^C"),
        ("", False, None, ""),
    ],
)
def test_record_on_failure(
    error: str,
    interrupt: bool,
    exit_code: int | None,
    output: str,
    env: dict[str, str],
    project: Path,
    data_dir: Path,
) -> None:
    run_hook("record", failure_payload(project, "pytest", error, is_interrupt=interrupt), env)

    [run] = _runs(data_dir)
    assert run["exit_code"] == exit_code
    assert run["output_tail"] == output
    assert run["is_test"] is True


def test_record_interrupted_success_has_no_exit_code(
    env: dict[str, str], project: Path, data_dir: Path
) -> None:
    payload = bash_payload(project, "sleep 100", tool_response={"stdout": "", "interrupted": True})
    run_hook("record", payload, env)

    assert _runs(data_dir)[0]["exit_code"] is None


def test_record_keeps_only_the_output_tail(
    env: dict[str, str], project: Path, data_dir: Path
) -> None:
    long = "x" * 5_000 + "END"
    run_hook("record", bash_payload(project, "c" * 3_000, tool_response={"stdout": long}), env)

    [run] = _runs(data_dir)
    assert run["output_tail"] == long[-4_000:]
    assert run["command"] == "c" * 2_000


def test_record_in_prompt_only_mode_never_writes_output(
    env: dict[str, str], project: Path, data_dir: Path
) -> None:
    env["CLAUDE_PLUGIN_OPTION_PROMPT_ONLY"] = "true"
    run_hook("record", bash_payload(project, "cat secrets.txt"), env)

    assert _runs(data_dir)[0]["output_tail"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"command": "x"}, "hook_event_name": "PostToolUse"},
        {"tool_name": "Bash", "tool_input": "ls", "hook_event_name": "PostToolUse"},
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "hook_event_name": "PreToolUse"},
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "hook_event_name": "PostToolUse",
            "session_id": "../x",
        },
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "hook_event_name": "PostToolUseFailure",
            "error": 5,
            "tool_response": None,
        },
    ],
)
def test_record_ignores_other_payloads(
    payload: dict[str, object], env: dict[str, str], data_dir: Path
) -> None:
    run_hook("record", {"session_id": "sess-1", **payload}, env)

    # Only the last payload is a run (an unreadable error: exit code unknown).
    runs = data_dir / "runs-sess-1"
    if payload.get("hook_event_name") == "PostToolUseFailure":
        assert _runs(data_dir)[0]["exit_code"] is None
    else:
        assert not runs.exists()


def test_record_without_data_dir_does_nothing(
    env: dict[str, str], project: Path, tmp_path: Path
) -> None:
    del env["CLAUDE_PLUGIN_DATA"]
    run_hook("record", bash_payload(project, "ls"), env)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "project"]


# --- after -------------------------------------------------------------------------


def test_after_sends_runs_and_clears_file(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    server.reply = {"message": "The tests ran. Nice.", "block_prompt": False, "prompt_only": False}
    run_hook("record", bash_payload(repo, "uv run pytest -q"), env)
    run_hook("record", failure_payload(repo, "ruff check", "Exit code 1\nE501"), env)
    run_hook("record", bash_payload(repo, "echo old turn", prompt_id="prompt-0"), env)

    out = run_hook("after", stop_payload(repo), env)

    assert json.loads(out) == {"systemMessage": "The tests ran. Nice."}
    [req] = server.requests
    assert req.path == "/v1/after"
    assert req.body["tool_runs"] == [
        {"command": "uv run pytest -q", "exit_code": 0, "output_tail": "out", "is_test": True},
        {"command": "ruff check", "exit_code": 1, "output_tail": "E501", "is_test": False},
    ]
    assert req.body["last_assistant_message"] == "Done: added the test."
    assert req.body["prompt_only"] is False
    assert req.body["agent"] == "claude-code"
    assert set(req.body) == {
        "agent",
        "plugin_version",
        "session_id",
        "prompt_id",
        "diff",
        "tool_runs",
        "last_assistant_message",
        "prompt_only",
        "capabilities",
    }
    assert not (data_dir / "runs-sess-1").exists()


def test_after_never_blocks_stop(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    server.reply = {"message": "odd", "block_prompt": True, "prompt_only": False}

    assert json.loads(run_hook("after", stop_payload(repo), env)) == {"systemMessage": "odd"}


def test_after_caps_runs_and_reply(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    for i in range(60):
        run_hook("record", bash_payload(repo, f"step {i}"), env)
    with (data_dir / "runs-sess-1").open("a") as f:
        f.write("not json\n[1]\n" + json.dumps({"prompt_id": "prompt-1", "command": 5}) + "\n")
        f.write(
            json.dumps(
                {"prompt_id": "prompt-1", "command": "x", "exit_code": True, "output_tail": 7}
            )
            + "\n"
        )

    run_hook("after", stop_payload(repo, last_assistant_message="r" * 25_000), env)

    body = server.requests[0].body
    assert len(body["tool_runs"]) == 50
    assert body["tool_runs"][-1] == {
        "command": "x",
        "exit_code": None,
        "output_tail": "",
        "is_test": False,
    }
    assert len(body["last_assistant_message"]) == 20_000


def test_after_skips_when_stop_hook_active(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    run_hook("record", bash_payload(repo, "ls"), env)

    assert run_hook("after", stop_payload(repo, stop_hook_active=True), env) == ""
    assert server.requests == []
    assert (data_dir / "runs-sess-1").exists()  # kept for the real end of the turn


def test_unknown_state_means_prompt_only(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    (repo / "app.py").write_text("print('v2')\n")
    run_hook(
        "record",
        bash_payload(repo, "python -c 'import secret'", tool_response={"stdout": "SECRET-OUT"}),
        env,
    )

    run_hook("after", stop_payload(repo), env)

    body = server.requests[0].body
    assert body["prompt_only"] is True
    assert body["diff"] is None
    assert body["tool_runs"] == [
        {"command": "python -c", "exit_code": 0, "output_tail": "", "is_test": False}
    ]
    assert body["last_assistant_message"] is None
    assert "SECRET-OUT" not in json.dumps(body)


def test_userconfig_prompt_only_wins(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    env["CLAUDE_PLUGIN_OPTION_PROMPT_ONLY"] = "true"
    (repo / "app.py").write_text("print('v2')\n")

    run_hook("after", stop_payload(repo), env)

    assert server.requests[0].body["diff"] is None
    assert server.requests[0].body["prompt_only"] is True


def test_after_caches_server_setting(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    server.reply = {"message": None, "block_prompt": False, "prompt_only": True}

    assert run_hook("after", stop_payload(repo), env) == ""
    assert json.loads((data_dir / "state.json").read_text())["prompt_only"] is True


def _edit_then_stop(repo: Path, env: dict[str, str], server: StubServer) -> str:
    """Before-hook, an edit by "the agent", then the Stop hook; the diff sent."""
    server.reply = dict(FULL)
    run_hook("before", prompt_payload(repo), env)
    (repo / "agent.py").write_text("print('agent')\n")
    run_hook("after", stop_payload(repo), env)
    diff = server.requests[-1].body["diff"]
    assert isinstance(diff, str)
    return diff


def test_diff_excludes_preexisting_changes(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    (repo / "app.py").write_text("print('the person edited this earlier')\n")
    (repo / "notes.txt").write_text("untracked, from before the turn\n")

    diff = _edit_then_stop(repo, env, server)

    assert "agent.py" in diff
    assert "+print('agent')" in diff
    assert "app.py" not in diff
    assert "notes.txt" not in diff
    assert not list(data_dir.glob("snap-*")) and not list(data_dir.glob("idx-*"))


def test_snapshot_never_touches_user_index(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    (repo / "staged.py").write_text("staged\n")
    git(repo, "add", "staged.py")
    (repo / "app.py").write_text("unstaged\n")
    index = repo / ".git" / "index"
    status_before = git(repo, "status", "--porcelain")  # may refresh the index itself
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    _edit_then_stop(repo, env, server)

    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert git(repo, "status", "--porcelain") == status_before + "?? agent.py\n"
    assert git(repo, "stash", "list") == ""


def test_snapshot_skips_credential_files(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    server.reply = dict(FULL)
    run_hook("before", prompt_payload(repo), env)
    secret = "AKIA-NEVER-SEND-0001"
    for name in (
        ".env",
        "deploy/.env.production",
        "certs/site.PEM",
        "id_ed25519",
        ".aws/credentials",
        "x.tfvars.json",
    ):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(f"{secret}\n")
    (repo / "agent.py").write_text("ok\n")

    run_hook("after", stop_payload(repo), env)

    diff = server.requests[-1].body["diff"]
    assert "agent.py" in diff
    assert secret not in diff


def test_unmatched_stop_falls_back_to_head_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    """A ``!`` command fires Stop with a prompt_id no before-hook saw: normal."""
    _state(data_dir, prompt_only=False)
    server.reply = dict(FULL)
    run_hook("before", prompt_payload(repo), env)  # snapshot for prompt-1
    (repo / "app.py").write_text("print('v2')\n")

    out = run_hook("after", stop_payload(repo, prompt_id="bang-command"), env)

    assert out == ""
    body = server.requests[-1].body
    assert body["prompt_id"] == "bang-command"
    assert "+print('v2')" in body["diff"]  # git diff HEAD
    assert not list(data_dir.glob("snap-*"))  # the stale snapshot is gone


def test_blocked_prompt_keeps_no_snapshot(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    server.reply = {"message": "Level set.", "block_prompt": True, "prompt_only": False}

    run_hook("before", prompt_payload(repo, "coach, quieter"), env)

    assert not list(data_dir.glob("snap-*"))


def test_server_failure_keeps_snapshot(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    server.mode = "status"
    server.status = 503

    run_hook("before", prompt_payload(repo), env)

    assert len(list(data_dir.glob("snap-*"))) == 1


def test_person_prompt_only_takes_no_snapshot(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    env["CLAUDE_PLUGIN_OPTION_PROMPT_ONLY"] = "1"
    server.reply = dict(FULL)

    run_hook("before", prompt_payload(repo), env)

    assert not list(data_dir.glob("snap-*"))


def _make(kind: str, project: Path) -> None:
    if kind == "not_git":
        return
    git(project, "init", "-q")
    if kind == "no_commits":
        (project / "new.py").write_text("print('new')\n")
        (project / "untracked.py").write_text("x\n")
        (project / ".env").write_text("SECRET=1\n")
        git(project, "add", "new.py")
        return
    (project / "app.py").write_text("v1\n")
    (project / ".env").write_text("SECRET=old\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    if kind == "binary":
        (project / "blob.bin").write_bytes(bytes(range(256)) * 10)
        git(project, "add", "blob.bin")
    elif kind == "huge":
        (project / "app.py").write_text("line of text\n" * 170_000)  # about 2 MB
    elif kind == "env_modified":
        (project / ".env").write_text("SECRET=new-and-private\n")
        (project / "app.py").write_text("v2\n")


@pytest.mark.parametrize("kind", ["not_git", "no_commits", "binary", "huge", "env_modified"])
def test_diff_collection_edge_cases(kind: str, project: Path) -> None:
    _make(kind, project)

    diff = gitdiff.collect_diff(project)

    if kind == "not_git":
        assert diff is None
        return
    assert diff is not None
    if kind == "no_commits":
        assert "+print('new')" in diff
        assert diff.startswith("Untracked files:\n  untracked.py\n")
        assert ".env" not in diff
    elif kind == "binary":
        assert "Binary files" in diff
    elif kind == "huge":
        assert len(diff) == 300_000
        assert diff.endswith(gitdiff.TRUNCATED)
    else:
        assert "+v2" in diff
        assert "SECRET" not in diff and ".env" not in diff


def test_after_server_failure_is_silent(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _state(data_dir, prompt_only=False)
    server.mode = "raw"
    server.raw = b"<html>bad gateway</html>"
    run_hook("record", bash_payload(repo, "ls"), env)

    started = time.monotonic()
    assert run_hook("after", stop_payload(repo), env) == ""
    assert time.monotonic() - started < 5

    assert not (data_dir / "runs-sess-1").exists()
    assert json.loads((data_dir / "state.json").read_text()) == {"prompt_only": False}


def test_after_without_token_sends_nothing_but_cleans_up(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]
    run_hook("record", bash_payload(repo, "ls"), env)

    assert run_hook("after", stop_payload(repo), env) == ""
    assert server.requests == []
    assert not (data_dir / "runs-sess-1").exists()


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", json.dumps({"session_id": "s"})])
def test_after_bad_payload_is_silent(payload: str, server: StubServer, env: dict[str, str]) -> None:
    assert run_hook("after", payload, env) == ""
    assert server.requests == []


def test_after_bad_ids_are_silent(server: StubServer, env: dict[str, str], repo: Path) -> None:
    assert run_hook("after", stop_payload(repo, prompt_id="has space"), env) == ""
    assert server.requests == []
