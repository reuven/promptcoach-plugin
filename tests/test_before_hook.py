"""Task 15: the before-prompt hook (``UserPromptSubmit`` → ``/v1/before``)."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from conftest import TOKEN, StubServer, prompt_payload, run_hook
from promptcoach_client import core
from promptcoach_client.claude_code import PROFILE
from promptcoach_client.setup_scan import SetupSources, collect_setup, gitignore_covers_env


def test_before_posts_and_emits_system_message(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    server.reply = {"message": "Say which file.", "block_prompt": False, "prompt_only": False}

    out = run_hook("before", prompt_payload(project, "fix it"), env)

    assert json.loads(out) == {"systemMessage": "Say which file."}
    [req] = server.requests
    assert req.path == "/v1/before"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.body["agent"] == "claude-code"
    assert req.body["plugin_version"] == "0.1.0"
    assert req.body["session_id"] == "sess-1"
    assert req.body["prompt_id"] == "prompt-1"
    assert req.body["prompt"] == "fix it"
    assert req.body["capabilities"] == {
        "can_block_prompt": True,
        "has_reply": True,
        "has_exit_codes": True,
    }
    assert set(req.body) == {
        "agent",
        "plugin_version",
        "session_id",
        "prompt_id",
        "prompt",
        "setup",
        "capabilities",
    }
    # The server's setting is cached for the after-hook.
    assert json.loads((data_dir / "state.json").read_text())["prompt_only"] is False


def test_block_reply_emits_decision_block(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = {
        "message": "Coach level set to teach.",
        "block_prompt": True,
        "prompt_only": True,
    }

    out = run_hook("before", prompt_payload(project, "coach, teach"), env)

    assert json.loads(out) == {"decision": "block", "reason": "Coach level set to teach."}


def test_silent_reply_prints_nothing(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    assert run_hook("before", prompt_payload(project), env) == ""
    assert len(server.requests) == 1


def _refused_url() -> str:
    """A localhost URL with nothing listening on it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.mark.parametrize(
    "failure", ["refused", "500", "malformed", "sleep", "redirect", "not_object"]
)
def test_server_failure_is_silent(
    failure: str, server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    if failure == "refused":
        env["CLAUDE_PLUGIN_OPTION_SERVER_URL"] = _refused_url()
    server.mode = {"500": "status", "malformed": "raw", "not_object": "raw"}.get(failure, failure)
    server.status = 500 if failure == "500" else 200
    server.raw = b"[1, 2]" if failure == "not_object" else b"{not json"

    started = time.monotonic()
    out = run_hook("before", prompt_payload(project), env)
    elapsed = time.monotonic() - started

    assert out == ""
    assert elapsed < 2.0
    assert not (data_dir / "state.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://coach.example.com",
        "http://localhost.example.com",
        "ftp://127.0.0.1",
        "https://user:pw@coach.example.com",
        "https://coach.example.com/?x=1",
        "https://coach.example.com/#frag",
        "https://",
        "http://[::1",
    ],
)
def test_refuses_plain_http_remote(
    url: str,
    server: StubServer,
    env: dict[str, str],
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(core._OPENER, "open", lambda *a, **k: opened.append(a))
    env["CLAUDE_PLUGIN_OPTION_SERVER_URL"] = url

    assert run_hook("before", prompt_payload(project), env) == ""
    assert opened == []


@pytest.mark.parametrize(
    "url", ["http://localhost:8000", "http://127.0.0.1:8000/", "https://coach.lernerpython.com"]
)
def test_allowed_urls(url: str) -> None:
    assert core.endpoint(url, "/v1/before") == url.rstrip("/") + "/v1/before"


def test_default_server_is_https(
    env: dict[str, str], project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []
    monkeypatch.setattr(core, "post", lambda url, *a: urls.append(url))
    del env["CLAUDE_PLUGIN_OPTION_SERVER_URL"]

    run_hook("before", prompt_payload(project), env)

    assert urls == ["https://coach.lernerpython.com/v1/before"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_collect_setup_detects_tools(project: Path, home: Path) -> None:
    _write(
        project / "pyproject.toml",
        '[project]\ndependencies = ["pandas>=2", "scikit-learn"]\n'
        "[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n[tool.pytest.ini_options]\n",
    )
    _write(project / "uv.lock", "")
    _write(project / ".github/workflows/ci.yml", "on: push\n")
    _write(project / "CLAUDE.md", "Use uv. Keep it short.\n")
    _write(project / ".pre-commit-config.yaml", "repos: []\n")
    _write(project / ".gitignore", ".env\ndata/\n*.csv\n*.tsv\n*.parquet\n*.xlsx\n*.dta\n*.sav\n")
    (project / "tests").mkdir()
    _write(project / ".claude/skills/release/SKILL.md", "x")
    _write(project / ".agents/skills/review/SKILL.md", "x")
    _write(project / "notebooks/explore.ipynb", "{}")
    hooks = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uv run ruff check --fix #pc:lint_after_edit",
                        }
                    ],
                },
                {
                    "hooks": [
                        {"type": "command", "command": "uv", "args": ["run", "ruff", "format"]}
                    ]
                },
            ],
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "./guard.sh --secret-token=abc"}]}
            ],
            "SessionStart": [{"hooks": [{"type": "command", "command": "mypy ."}]}],
        }
    }
    _write(project / ".claude/settings.json", json.dumps(hooks))

    setup = collect_setup(project, home, PROFILE.sources)

    assert setup == {
        "instructions": {"CLAUDE.md": "Use uv. Keep it short.\n"},
        "gitignore_covers_env": True,
        "has_tests_dir": True,
        "test_command": "uv run pytest",
        "has_ci": True,
        "has_precommit": True,
        "linters": ["mypy", "ruff"],
        "hook_events": ["PostToolUse", "PreToolUse", "SessionStart"],
        "hook_tools": {
            "PostToolUse": ["pc:lint_after_edit", "ruff", "ruff-check", "ruff-format", "uv"]
        },
        "project_signals": ["jupyter", "pandas", "sklearn"],
        "gitignore_covers_data": True,
        "skills": ["release", "review"],
    }
    # Only tool names leave the machine, never the commands.
    assert "secret-token" not in json.dumps(setup)


def test_collect_setup_never_reads_env_files(
    server: StubServer, env: dict[str, str], project: Path, home: Path
) -> None:
    secret = "sk-live-NEVER-SEND-THIS-1234"
    for name in (".env", ".env.local", "config/.env", ".envrc"):
        _write(project / name, f"API_KEY={secret}\n")
    _write(project / "CLAUDE.md", "Never commit .env\n")

    run_hook("before", prompt_payload(project), env)

    [req] = server.requests
    assert secret not in json.dumps(req.body)


def test_collect_setup_reads_global_instructions(project: Path, home: Path) -> None:
    _write(home / ".claude/CLAUDE.md", "Always use uv.\n")
    _write(home / ".pi/agent/AGENTS.md", "Pi rules\n")
    _write(home / ".gemini/GEMINI.md", "not read by Claude Code\n")
    _write(project / "AGENTS.md", "Project rules\n")

    setup = collect_setup(project, home, PROFILE.sources)

    assert setup["instructions"] == {
        "AGENTS.md": "Project rules\n",
        "~/.claude/CLAUDE.md": "Always use uv.\n",
        "~/.pi/agent/AGENTS.md": "Pi rules\n",
    }


def test_instruction_files_are_capped(project: Path, home: Path) -> None:
    _write(project / "CLAUDE.md", "a" * 25_000)
    sources = SetupSources(
        project_instruction_files=(
            "CLAUDE.md",
            "AGENTS.md",
            "SYSTEM.md",
            ".claude/CLAUDE.md",
            "GEMINI.md",
            "AGENTS.override.md",
            "APPEND_SYSTEM.md",
            ".github/copilot-instructions.md",
        ),
        global_instruction_files=(),
        hook_settings_files=(),
        hook_tools_keys=frozenset(),
        skills_dirs=(),
    )
    for name in sources.project_instruction_files[1:]:
        _write(project / name, "b" * 20_000)

    found = collect_setup(project, home, sources)["instructions"]

    assert isinstance(found, dict)
    assert len(found["CLAUDE.md"]) == 20_000
    assert sum(len(v) for v in found.values()) == 140_000
    assert ".github/copilot-instructions.md" not in found  # no room left


@pytest.mark.parametrize(
    ("lines", "covered"),
    [
        (".env\n", True),
        (".env*\n", True),
        ("*.env\n", True),
        ("/.env\n", True),
        ("  .env  \n", True),
        ("node_modules/\n", False),
        (".envrc\n", False),
        ("# .env\n", False),
        (None, None),
    ],
)
def test_gitignore_covers_env_variants(
    lines: str | None, covered: bool | None, project: Path, home: Path
) -> None:
    if lines is not None:
        _write(project / ".gitignore", lines)

    assert collect_setup(project, home, PROFILE.sources)["gitignore_covers_env"] is covered
    assert gitignore_covers_env(None) is None


def test_missing_token_message_once_per_session(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]

    first = run_hook("before", prompt_payload(project), env)
    second = run_hook("before", prompt_payload(project, prompt_id="prompt-2"), env)
    other_session = run_hook("before", prompt_payload(project, session_id="sess-2"), env)

    message = "Prompt coach: add your token with /plugin → promptcoach → Configure."
    assert json.loads(first) == {"systemMessage": message}
    assert second == ""
    assert json.loads(other_session) == {"systemMessage": message}
    assert server.requests == []  # no token, no network


def test_missing_token_without_data_dir_stays_quiet(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]
    del env["CLAUDE_PLUGIN_DATA"]

    assert run_hook("before", prompt_payload(project), env) == ""


def test_blank_token_counts_as_missing(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    env["CLAUDE_PLUGIN_OPTION_TOKEN"] = "   "

    assert "add your token" in run_hook("before", prompt_payload(project), env)
    assert server.requests == []
