"""Edge cases of the client's parts, one at a time."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import time
import types
from pathlib import Path
from typing import Any

import pytest

from conftest import StubServer, git, posix_only, prompt_payload, run_hook, shown
from promptcoach_client import claude_code, core, gitdiff, setup_scan
from promptcoach_client.claude_code import PROFILE, config_from_env, emit, main, tool_ran
from promptcoach_client.setup_scan import SetupSources, collect_setup, tools_in

# --- core --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "reply"),
    [
        (None, None),
        ({"message": "x", "prompt_only": False}, core.Reply("x", False, False)),
        ({"message": "", "prompt_only": True}, core.Reply(None, False, True)),
        ({"message": None}, None),
        ({"message": None, "prompt_only": "no"}, None),
        ({"message": None, "block_prompt": 1, "prompt_only": False}, None),
        ({"message": 5, "prompt_only": False}, None),
        ({"message": None, "block_prompt": True, "prompt_only": False}, None),
    ],
)
def test_parse_reply(data: dict[str, Any] | None, reply: core.Reply | None) -> None:
    assert core.parse_reply(data) == reply


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("", False),
        (None, False),
        ("2", False),
    ],
)
def test_parse_bool(text: str | None, value: bool) -> None:
    assert core.parse_bool(text) is value


def test_post_without_url_does_nothing() -> None:
    assert core.post(None, "t", {}, 1.0) is None


def test_post_empty_and_oversized_bodies(server: StubServer) -> None:
    server.mode = "raw"
    server.raw = b""
    assert core.post(server.url + "/v1/before", "t", {}, 2.0) is None
    server.raw = b'{"a": "' + b"x" * core.MAX_REPLY_BYTES + b'"}'
    assert core.post(server.url + "/v1/before", "t", {}, 2.0) is None
    server.mode, server.status = "status", 204
    assert core.post(server.url + "/v1/before", "t", {}, 2.0) is None


def _profile(**changes: Any) -> core.AgentProfile:
    fields = {f.name: getattr(PROFILE, f.name) for f in dataclasses.fields(PROFILE)}
    return core.AgentProfile(**{**fields, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"agent": "vim"},
        {"plugin_version": "1.0.0-beta"},
        {"missing_token_message": ""},
        {"command_without_token_message": ""},
        {"sources": dataclasses.replace(PROFILE.sources, project_instruction_files=("x.md",))},
        {"sources": dataclasses.replace(PROFILE.sources, global_instruction_files=("~/x.md",))},
    ],
)
def test_profile_refuses_what_the_server_would(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _profile(**changes)


def test_store_without_data_dir() -> None:
    store = core.Store(None)
    assert store.server_prompt_only() is None
    store.set_server_prompt_only(True)
    assert store.first_warning("s") is False
    store.save_snapshot("s", "p", "a" * 40)
    assert store.take_snapshot("s", "p") is None
    store.append_run("s", {})
    assert store.take_runs("s") == []
    store.sweep(time.time())


def test_store_hashes_unsafe_session_ids(tmp_path: Path) -> None:
    store = core.Store(tmp_path)
    store.save_snapshot("a:b.c", "p", "a" * 40)
    store.save_snapshot("abc", "p", "a" * 40)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 2 and "snap-abc" in names
    assert all(n.startswith("snap-") and ":" not in n for n in names)


def test_store_tolerates_bad_files(tmp_path: Path) -> None:
    store = core.Store(tmp_path)
    (tmp_path / "state.json").write_text("[1]")
    assert store.server_prompt_only() is None
    (tmp_path / "state.json").write_text("{bad")
    assert store.server_prompt_only() is None
    (tmp_path / "state.json").write_text('{"prompt_only": "yes"}')
    assert store.server_prompt_only() is None
    store.save_snapshot("s", "p", "not-a-sha")
    assert store.take_snapshot("s", "p") is None
    assert not (tmp_path / "snap-s").exists()


def test_store_write_failure_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    core.Store(tmp_path).set_server_prompt_only(True)
    assert list(tmp_path.iterdir()) == []


def test_first_warning_when_state_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = core.Store(tmp_path)
    monkeypatch.setattr(store, "_write_json", lambda *a: None)
    assert store.first_warning("s") is False  # can't remember it, so don't say it


def test_take_runs_when_the_file_vanishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = core.Store(tmp_path)
    assert store.take_runs("s") == []  # no file


def test_sweep_removes_only_old_temp_files(tmp_path: Path) -> None:
    old = time.time() - core.STALE_AFTER_S - 10
    names = ["snap-a", "idx-a", "runs-a", ".taken-runs-a-1", ".tmp-x", "state.json", "snap-new"]
    for name in names:
        (tmp_path / name).write_text("x")
        if name != "snap-new":
            os.utime(tmp_path / name, (old, old))

    core.Store(tmp_path).sweep(time.time())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["snap-new", "state.json"]


def test_sweep_tolerates_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core.Store(tmp_path / "missing").sweep(time.time())
    (tmp_path / "snap-a").write_text("x")

    def fail(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(Path, "stat", fail)
    core.Store(tmp_path).sweep(time.time())
    monkeypatch.undo()
    assert (tmp_path / "snap-a").exists()


def test_before_with_bad_ids_is_silent(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    assert run_hook("before", prompt_payload(project, prompt_id="a b"), env) == ""
    assert server.requests == []


def test_before_without_data_dir_still_coaches(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    del env["CLAUDE_PLUGIN_DATA"]
    server.reply = {"message": "ok", "block_prompt": False, "prompt_only": False}
    assert json.loads(run_hook("before", prompt_payload(project), env)) == shown("ok")
    # Unknown setting: the setup is sent as prompt-only mode sends it.
    assert server.requests[0].body["setup"]["test_command"] is None


def test_prompt_is_capped(server: StubServer, env: dict[str, str], project: Path) -> None:
    run_hook("before", prompt_payload(project, "p" * 100_005), env)
    assert len(server.requests[0].body["prompt"]) == 100_000


# --- adapter -----------------------------------------------------------------------


def test_config_from_env_defaults(tmp_path: Path) -> None:
    config = config_from_env({})
    assert config.token is None
    assert config.server_url == "https://coach.lernerpython.com"
    assert config.prompt_only is False
    assert config.data_dir is None
    assert config.home == Path.home()


def test_unknown_mode_and_bad_stdin(env: dict[str, str]) -> None:
    out = io.StringIO()
    assert main(["bogus"], io.StringIO("{}"), out, env) == 0
    assert main([], io.StringIO("{}"), out, env) == 0
    assert main(["before"], io.StringIO("[]"), out, env) == 0
    assert out.getvalue() == ""


def test_tool_ran_ignores_non_string_prompt_id() -> None:
    run = tool_ran(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "hook_event_name": "PostToolUse",
            "session_id": "s",
            "prompt_id": 5,
            "tool_response": "odd",
        }
    )
    assert run is not None and run.prompt_id is None and run.output == "" and run.exit_code == 0


TOP = "[Coach] " + "─" * 32
BOTTOM = "─" * 40


def test_frame_sets_the_coach_off_from_claude_codes_label() -> None:
    """Claude Code shows "Stop says: " before a hook's message; "Stop says:
    Coach: ..." was confusing, so the message gets a header and a rule."""
    assert claude_code.frame("Coach: add a test.") == f"{TOP}\nadd a test.\n{BOTTOM}"


def test_frame_drops_coach_at_the_start_of_every_line_only() -> None:
    message = "A sharper version: x\nCoach: one.\n  Why: Coach: stays.\nCoach: two."
    assert claude_code.frame(message) == (
        f"{TOP}\nA sharper version: x\none.\n  Why: Coach: stays.\ntwo.\n{BOTTOM}"
    )


def test_frame_keeps_the_whole_message_within_4_kb() -> None:
    framed = claude_code.frame("Coach: " + "é" * 5_000)
    assert len(framed.encode()) <= core.MAX_MESSAGE_BYTES
    assert framed.startswith(f"{TOP}\né") and framed.endswith(f"\n{BOTTOM}")


def test_emit_frames_both_shown_and_blocking_messages() -> None:
    out = io.StringIO()
    emit(core.Reply("Coach: hi.", False, False), out, allow_block=True)
    assert json.loads(out.getvalue()) == {"systemMessage": f"{TOP}\nhi.\n{BOTTOM}"}
    out = io.StringIO()
    emit(core.Reply("Coach: hi.", True, False), out, allow_block=True)
    assert json.loads(out.getvalue())["reason"] == f"{TOP}\nhi.\n{BOTTOM}"


def test_emit_writes_nothing_for_silence() -> None:
    out = io.StringIO()
    emit(None, out, allow_block=True)
    emit(core.Reply(None, False, False), out, allow_block=True)
    assert out.getvalue() == ""


# --- git ---------------------------------------------------------------------------


def test_run_git_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert gitdiff.run_git(["status"], tmp_path) is None
    assert gitdiff.collect_diff(tmp_path) is None


def test_run_git_timeout_and_limit(repo: Path) -> None:
    (repo / "big.txt").write_text("x\n" * 50_000)
    git(repo, "add", "big.txt")
    out = gitdiff.run_git(["diff", "--cached"], repo, limit=1_000)
    assert out is not None and out.truncated and len(out.text) == 1_000
    assert gitdiff.run_git(["diff", "--cached"], repo, timeout=0.0) is None


def test_run_git_ignores_inherited_git_variables(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    assert gitdiff.repo_root(repo) == repo.resolve()


def test_snapshot_outside_a_repo(tmp_path: Path) -> None:
    assert gitdiff.snapshot(tmp_path, tmp_path / "data", gitdiff.Deadline(10)) is None
    assert not (tmp_path / "data").exists()


def test_snapshot_in_a_fresh_repo_has_no_index_to_copy(project: Path, tmp_path: Path) -> None:
    git(project, "init", "-q")
    (project / "a.py").write_text("a\n")
    tree = gitdiff.snapshot(project, tmp_path / "data", gitdiff.Deadline(10))
    assert tree is not None and len(tree) == 40


@pytest.mark.parametrize(
    "failing", ["config --show-scope", "rev-parse --git-path", "add", "write-tree"]
)
def test_snapshot_git_failures(
    failing: str, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gitdiff.run_git

    def fake(args: list[str], cwd: Path, **kwargs: Any) -> gitdiff.GitOutput | None:
        return None if " ".join(args).startswith(failing) else real(args, cwd, **kwargs)

    monkeypatch.setattr(gitdiff, "run_git", fake)
    assert gitdiff.snapshot(repo, tmp_path / "data", gitdiff.Deadline(10)) is None
    assert list((tmp_path / "data").glob("*")) == []


def test_snapshot_os_error(repo: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert gitdiff.snapshot(repo, blocker / "data", gitdiff.Deadline(10)) is None


def test_diff_trees_outside_a_repo(tmp_path: Path) -> None:
    assert gitdiff.diff_trees(tmp_path, "a" * 40, "b" * 40, gitdiff.Deadline(10)) is None


def test_collect_diff_no_commits_cases(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git(project, "init", "-q")
    (project / "a.py").write_text("a\n")
    git(project, "add", "a.py")
    assert gitdiff.collect_diff(project) is not None and "Untracked" not in str(
        gitdiff.collect_diff(project)
    )
    real = gitdiff.run_git

    def no_diff(args: list[str], cwd: Path, **kwargs: Any) -> gitdiff.GitOutput | None:
        return None if args[0] == "diff" or args[0] == "ls-files" else real(args, cwd, **kwargs)

    monkeypatch.setattr(gitdiff, "run_git", no_diff)
    assert gitdiff.collect_diff(project) is None


def test_after_falls_back_when_the_end_snapshot_fails(
    server: StubServer,
    env: dict[str, str],
    repo: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir.mkdir()
    (data_dir / "state.json").write_text('{"prompt_only": false}')
    run_hook("before", prompt_payload(repo), env)
    (repo / "app.py").write_text("changed\n")
    monkeypatch.setattr(gitdiff, "snapshot", lambda *a, **k: None)

    run_hook("after", {"session_id": "sess-1", "prompt_id": "prompt-1", "cwd": str(repo)}, env)

    assert "+changed" in server.requests[-1].body["diff"]


# --- setup scan --------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


SOURCES = PROFILE.sources


def test_setup_of_an_empty_project(project: Path, home: Path) -> None:
    assert collect_setup(project, home, SOURCES) == {
        "instructions": {},
        "gitignore_covers_env": None,
        "has_tests_dir": False,
        "test_command": None,
        "has_ci": False,
        "has_precommit": False,
        "linters": [],
        "hook_events": [],
        "hook_tools": {},
        "project_signals": [],
        "gitignore_covers_data": None,
        "skills": [],
    }


@pytest.mark.parametrize(
    ("files", "command"),
    [
        ({"CLAUDE.md": "run pytest -q"}, "pytest"),
        ({"AGENTS.md": "Run tests with uv run pytest."}, "uv run pytest"),
        ({"pyproject.toml": "[tool.pytest.ini_options]\n"}, "pytest"),
        ({"Makefile": "lint:\n\truff\ntest:\n\tpytest\n"}, "make test"),
        ({"Makefile": "lint:\n\truff\n"}, None),
    ],
)
def test_test_command_sources(
    files: dict[str, str], command: str | None, project: Path, home: Path
) -> None:
    for name, text in files.items():
        _write(project / name, text)
    assert collect_setup(project, home, SOURCES)["test_command"] == command


def test_test_command_in_prompt_only_mode(project: Path, home: Path) -> None:
    _write(project / "Makefile", "test:\n\tpytest\n")
    _write(project / ".claude/skills/odd$name/x", "")
    _write(project / ".claude/skills/fine/x", "")
    setup = collect_setup(project, home, SOURCES, prompt_only=True)
    assert setup["test_command"] == "make test"
    assert setup["skills"] == ["fine"]


def test_linters_from_config_files(project: Path, home: Path) -> None:
    for name in ("ruff.toml", "mypy.ini", "pyrightconfig.json", ".flake8", "pylintrc"):
        _write(project / name, "")
    _write(project / "pyproject.toml", "[tool.black]\n")
    assert collect_setup(project, home, SOURCES)["linters"] == [
        "black",
        "flake8",
        "mypy",
        "pylint",
        "pyright",
        "ruff",
    ]


def test_linters_from_setup_cfg_and_tox(project: Path, home: Path) -> None:
    _write(project / "setup.cfg", "[mypy]\n")
    _write(project / "tox.ini", "[flake8]\n")
    assert collect_setup(project, home, SOURCES)["linters"] == ["flake8", "mypy"]


def test_hook_settings_edge_cases(project: Path, home: Path) -> None:
    _write(project / ".claude/settings.json", "{not json")
    _write(
        project / ".claude/settings.local.json",
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                "odd",
                                {"command": "uv run pytest -q #pc:tests_before_done #pc:unknown"},
                            ]
                        },
                        "odd",
                        {"hooks": "odd"},
                    ],
                    "bad name!": [],
                    "PreToolUse": "odd",
                    "Notification": [{"hooks": [{"command": "say done"}]}],
                }
            }
        ),
    )
    _write(home / ".claude/settings.json", json.dumps({"hooks": ["not", "a", "dict"]}))
    setup = collect_setup(project, home, SOURCES)
    assert setup["hook_events"] == ["Notification", "PreToolUse", "Stop"]
    assert setup["hook_tools"] == {"Stop": ["pc:tests_before_done", "pytest", "uv"]}


def test_hook_settings_not_an_object(project: Path, home: Path) -> None:
    _write(project / ".claude/settings.json", "[1]")
    assert collect_setup(project, home, SOURCES)["hook_events"] == []


@pytest.mark.parametrize(
    ("command", "tools"),
    [
        ("pre-commit run --all-files", {"pre-commit"}),
        ("pip install x && black .", {"pip", "black"}),
        ("uvx ruff check", {"ruff", "ruff-check"}),
        ("npx prettier --write && eslint .", {"prettier", "eslint"}),
        ("mypyc build; pyright", {"pyright"}),
        ("my.pytest-helper", set()),
    ],
)
def test_tools_in(command: str, tools: set[str]) -> None:
    assert tools_in(command) == tools


def test_project_signals(project: Path, home: Path) -> None:
    _write(
        project / "pyproject.toml", 'dependencies = ["numpy-financial", "polars>=1", "Plotly"]\n'
    )
    _write(project / "requirements.txt", "# seaborn\nmatplotlib==3\n\n")
    _write(project / "requirements-dev.txt", "marimo\n")
    _write(project / "nb.py", "import os\n")
    assert collect_setup(project, home, SOURCES)["project_signals"] == [
        "marimo",
        "matplotlib",
        "plotly",
        "polars",
    ]


def test_marimo_notebooks_and_walk_limits(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(project / "notebooks/a.py", "import marimo\napp = marimo.App()\n")
    _write(project / ".venv/lib/x.ipynb", "{}")  # hidden: skipped
    _write(project / "a/b/c/d/deep.ipynb", "{}")  # too deep: skipped
    assert collect_setup(project, home, SOURCES)["project_signals"] == ["marimo"]
    monkeypatch.setattr(setup_scan, "MAX_WALK_FILES", 0)
    assert collect_setup(project, home, SOURCES)["project_signals"] == []


def test_gitignore_covers_data_needs_everything(project: Path, home: Path) -> None:
    _write(project / ".gitignore", "data/\n*.csv\n")
    assert collect_setup(project, home, SOURCES)["gitignore_covers_data"] is False


def test_skills_are_capped_and_deduplicated(project: Path, home: Path) -> None:
    for i in range(55):
        (project / ".claude/skills" / f"s{i:02}").mkdir(parents=True)
    (project / ".agents/skills/s00").mkdir(parents=True)
    (project / ".agents/skills/").joinpath("x" * 81).mkdir()
    (project / ".agents/skills/file.md").write_text("")
    skills = collect_setup(project, home, SOURCES)["skills"]
    assert isinstance(skills, list) and len(skills) == 50 and skills[0] == "s00"


@posix_only("test_windows.py::test_read_control_without_posix_open_flags", "FIFOs")
def test_read_text_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert setup_scan.read_text(tmp_path, tmp_path) is None  # a directory
    os.mkfifo(tmp_path / "fifo")
    assert setup_scan.read_text(tmp_path / "fifo", tmp_path) is None  # never blocks
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    assert setup_scan.read_text(tmp_path / "loop", tmp_path) is None
    (tmp_path / "f").write_text("x")
    real_open = os.open

    def fail(*args: object, **kwargs: object) -> int:
        raise OSError

    # Patched in the module only: pytest's own capture calls os.fstat.
    fake_os = types.SimpleNamespace(
        **{k: getattr(os, k) for k in dir(os) if not k.startswith("__")}
    )
    monkeypatch.setattr(setup_scan, "os", fake_os)
    monkeypatch.setattr(fake_os, "open", fail)
    assert setup_scan.read_text(tmp_path / "f", tmp_path) is None
    monkeypatch.setattr(fake_os, "open", real_open)
    monkeypatch.setattr(fake_os, "fstat", fail)
    assert setup_scan.read_text(tmp_path / "f", tmp_path) is None


def test_skills_dir_that_is_a_file(project: Path, home: Path) -> None:
    _write(project / ".claude/skills", "not a directory")
    assert collect_setup(project, home, SOURCES)["skills"] == []


def test_sources_only_name_known_hook_events() -> None:
    assert SetupSources((), (), (), frozenset(), ()).hook_tools_keys == frozenset()


@posix_only("test_windows.py::test_windows_git_that_hangs_is_killed_in_time", "a git written in sh")
def test_run_git_that_hangs_after_closing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexec >&-\n/bin/sleep 5\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    started = time.monotonic()
    # 1.5 s: time for the shell to close its output even on a loaded machine,
    # so the wait (not the read) is what times out.
    assert gitdiff.run_git(["status"], tmp_path, timeout=1.5) is None
    assert time.monotonic() - started < 4
