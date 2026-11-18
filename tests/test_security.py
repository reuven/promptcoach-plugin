"""Findings from the security review: symlinks, repo-local git config, deadlines."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from conftest import StubServer, git, prompt_payload, run_hook, stop_payload
from promptcoach_client import core
from promptcoach_client.claude_code import PROFILE, emit
from promptcoach_client.setup_scan import collect_setup

SECRET = "-----BEGIN OPENSSH PRIVATE KEY----- NEVER-SEND-ME"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- 1. symlinks never pull in files from elsewhere --------------------------------


def test_symlinked_instruction_file_outside_the_project_is_absent(
    project: Path, home: Path, tmp_path: Path
) -> None:
    _write(tmp_path / "elsewhere" / "id_rsa", SECRET)
    (project / "CLAUDE.md").symlink_to(tmp_path / "elsewhere" / "id_rsa")
    (project / "pyproject.toml").symlink_to(tmp_path / "elsewhere" / "id_rsa")

    setup = collect_setup(project, home, PROFILE.sources)

    assert "CLAUDE.md" not in setup["instructions"]  # type: ignore[operator]
    assert SECRET not in json.dumps(setup)


def test_symlink_to_a_forbidden_file_inside_the_project_is_absent(
    project: Path, home: Path
) -> None:
    _write(project / ".env", f"KEY={SECRET}\n")
    _write(project / "keys" / "deploy.pem", SECRET)
    (project / "AGENTS.md").symlink_to(project / ".env")
    (project / "CLAUDE.md").symlink_to(project / "keys" / "deploy.pem")

    setup = collect_setup(project, home, PROFILE.sources)

    assert setup["instructions"] == {}
    assert SECRET not in json.dumps(setup)


def test_symlinked_parent_directory_is_not_followed_out(
    project: Path, home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    _write(outside / "CLAUDE.md", SECRET)
    _write(
        outside / "settings.json",
        json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "pytest"}]}]}}),
    )
    (outside / "skills" / "private-skill-name").mkdir(parents=True)
    (project / ".claude").symlink_to(outside)

    setup = collect_setup(project, home, PROFILE.sources)

    assert setup["instructions"] == {}
    assert setup["hook_events"] == []
    assert setup["skills"] == []


def test_global_file_symlinked_out_of_home_is_absent(
    project: Path, home: Path, tmp_path: Path
) -> None:
    _write(tmp_path / "aws" / "credentials", SECRET)
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").symlink_to(tmp_path / "aws" / "credentials")

    assert collect_setup(project, home, PROFILE.sources)["instructions"] == {}


def test_symlink_within_the_project_still_works(project: Path, home: Path) -> None:
    _write(project / "AGENTS.md", "Shared rules\n")
    (project / "CLAUDE.md").symlink_to(project / "AGENTS.md")

    assert collect_setup(project, home, PROFILE.sources)["instructions"] == {
        "CLAUDE.md": "Shared rules\n",
        "AGENTS.md": "Shared rules\n",
    }


# --- 2. repo-local git config can't run commands ------------------------------------


def _evil_repo(repo: Path, tmp_path: Path, *, with_filter: bool) -> Path:
    marker = tmp_path / "pwned"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    script.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(script))
    if with_filter:
        git(repo, "config", "filter.evil.clean", str(script))
        git(repo, "config", "filter.evil.smudge", str(script))
        (repo / ".gitattributes").write_text("*.py filter=evil\n")
    return marker


@pytest.mark.parametrize("with_filter", [False, True])
def test_repo_local_git_config_never_runs(
    with_filter: bool,
    server: StubServer,
    env: dict[str, str],
    repo: Path,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    marker = _evil_repo(repo, tmp_path, with_filter=with_filter)
    data_dir.mkdir()
    (data_dir / "state.json").write_text('{"prompt_only": false}')

    run_hook("before", prompt_payload(repo), env)
    (repo / "app.py").write_text("print('v2')\n")
    assert run_hook("after", stop_payload(repo), env) == ""
    run_hook("after", stop_payload(repo, prompt_id="unmatched"), env)  # the fallback path too

    assert not marker.exists()
    diffs = [r.body["diff"] for r in server.requests if r.path == "/v1/after"]
    if with_filter:
        assert diffs == [None, None]  # a repo-defined filter: no diff at all, silently
    else:
        assert all("+print('v2')" in d for d in diffs), diffs


# --- break-check gaps: a trickling server, and Stop never blocks --------------------


def test_a_trickling_server_cannot_stretch_the_deadline(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.mode = "trickle"

    started = time.monotonic()
    assert run_hook("before", prompt_payload(project), env) == ""
    assert time.monotonic() - started < 2.5


def test_emit_never_blocks_where_blocking_is_not_allowed() -> None:
    out = io.StringIO()
    emit(core.Reply("x", True, False), out, allow_block=False)
    assert json.loads(out.getvalue()) == {"systemMessage": "x"}
