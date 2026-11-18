"""Task 15: the before-prompt hook (``UserPromptSubmit`` → ``/v1/before``)."""

from __future__ import annotations

import dataclasses
import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    COMMAND_PROMPTS,
    ORDINARY_PROMPTS,
    TOKEN,
    StubServer,
    prompt_payload,
    run_hook,
    stop_payload,
)
from promptcoach_client import claude_code, core
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

    out = run_hook("before", prompt_payload(project, "coach, louder"), env)

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


# --- a coach command fails closed when the server doesn't answer --------------------------

# Every way /v1/before can end with no usable reply: (stub mode, HTTP status).
NO_ANSWER = {
    "client_timeout": ("sleep", 200),
    "network_error": ("refused", 200),
    "status_500": ("status", 500),
    "status_503": ("status", 503),
    "malformed_json": ("raw", 200),
    "not_an_object": ("not_object", 200),
    "not_a_coach_reply": ("not_reply", 200),
    "not_http": ("badstatus", 200),
    "trickle": ("trickle", 200),
    "redirect": ("redirect", 200),
}


def _no_answer(kind: str, server: StubServer, env: dict[str, str]) -> None:
    """Make the stub (or the URL) fail the way ``kind`` names."""
    mode, server.status = NO_ANSWER[kind]
    if mode == "refused":
        env["CLAUDE_PLUGIN_OPTION_SERVER_URL"] = _refused_url()
    elif mode == "not_object":
        server.mode, server.raw = "raw", b"[1, 2]"
    elif mode == "not_reply":  # a block with no message: parse_reply refuses it
        server.reply = {"message": None, "block_prompt": True, "prompt_only": False}
    else:
        server.mode, server.raw = mode, b"{not json"


# The whole reason, fixed: what happened and what to do, in the server's voice
# (its own COMMAND_RETRY). Nothing the person typed, and no token.
REASON = "Coach: I didn't answer in time, so that command wasn't applied. Please send it again."
NOT_ANSWERED = {"decision": "block", "reason": REASON}


@pytest.mark.parametrize("kind", sorted(NO_ANSWER))
def test_command_is_blocked_when_the_server_does_not_answer(
    kind: str, server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    """The command was not applied, so it must not reach Claude as a prompt."""
    data_dir.mkdir()
    (data_dir / "state.json").write_text('{"prompt_only": false}')
    _no_answer(kind, server, env)

    started = time.monotonic()
    out = run_hook("before", prompt_payload(repo, "coach, quieter"), env)
    elapsed = time.monotonic() - started

    assert json.loads(out) == NOT_ANSWERED
    assert elapsed < core.BEFORE_BUDGET_S
    # A local block says nothing about the server's setting, and keeps no
    # snapshot: the prompt never reaches Claude, so no turn follows.
    assert json.loads((data_dir / "state.json").read_text()) == {"prompt_only": False}
    assert not list(data_dir.glob("snap-*"))


@pytest.mark.parametrize("kind", sorted(NO_ANSWER))
def test_ordinary_prompt_still_passes_when_the_server_does_not_answer(
    kind: str, server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    _no_answer(kind, server, env)

    assert run_hook("before", prompt_payload(project, "add a test"), env) == ""
    assert not (data_dir / "state.json").exists()


@pytest.mark.parametrize("prompt", COMMAND_PROMPTS)
def test_every_command_form_fails_closed(prompt: str, env: dict[str, str], project: Path) -> None:
    env["CLAUDE_PLUGIN_OPTION_SERVER_URL"] = _refused_url()

    assert json.loads(run_hook("before", prompt_payload(project, prompt), env)) == NOT_ANSWERED


@pytest.mark.parametrize("prompt", ORDINARY_PROMPTS)
def test_a_near_miss_is_not_blocked_locally(
    prompt: str, env: dict[str, str], project: Path
) -> None:
    env["CLAUDE_PLUGIN_OPTION_SERVER_URL"] = _refused_url()

    assert run_hook("before", prompt_payload(project, prompt), env) == ""


@pytest.mark.parametrize("prompt", COMMAND_PROMPTS)
def test_the_local_block_echoes_nothing(prompt: str, env: dict[str, str], project: Path) -> None:
    env["CLAUDE_PLUGIN_OPTION_SERVER_URL"] = _refused_url()

    out = run_hook("before", prompt_payload(project, prompt), env)

    assert json.loads(out)["reason"] == REASON
    assert TOKEN not in out
    assert " ".join(prompt.split()).lower() not in out.lower()


def test_a_server_block_still_wins(server: StubServer, env: dict[str, str], project: Path) -> None:
    server.reply = {"message": "Coach: quieter now.", "block_prompt": True, "prompt_only": False}

    out = json.loads(run_hook("before", prompt_payload(project, "coach, quieter"), env))

    assert out == {"decision": "block", "reason": "Coach: quieter now."}


def test_no_local_block_for_an_agent_that_cannot_block(env: dict[str, str], project: Path) -> None:
    """An agent that can't keep a prompt back gets no block it couldn't honor."""
    env["CLAUDE_PLUGIN_OPTION_SERVER_URL"] = _refused_url()
    profile = dataclasses.replace(
        PROFILE, capabilities=core.Capabilities(False, has_reply=True, has_exit_codes=True)
    )
    event = core.PromptSubmitted("sess-1", "prompt-1", "coach, quieter", project, project)
    config = claude_code.config_from_env(env)

    assert core.before(event, config, profile) is None


# --- a coach command never reaches the agent, whatever the server replies -----------------

# The server's non-blocking replies (PromptCoach-v1 api.py): the pool-timeout and
# rate-limit quiet replies, and the sign-in and lapsed messages.
SIGN_IN = (
    "Prompt coach: sign in at https://coach.lernerpython.com/signin to get your token, "
    "and coaching will start right away."
)
LAPSED = (
    "Prompt coach: your LernerPython membership isn't active right now. "
    "Renew at https://lernerpython.com and coaching picks right back up."
)
NON_BLOCKING: dict[str, dict[str, Any]] = {
    "quiet_pool_timeout": {"message": None, "block_prompt": False, "prompt_only": True},
    "rate_limited": {"message": None, "block_prompt": False, "prompt_only": False},
    "sign_in": {"message": SIGN_IN, "block_prompt": False, "prompt_only": True},
    "lapsed": {"message": LAPSED, "block_prompt": False, "prompt_only": True},
}
NO_TOKEN_REASON = (
    "Coach: that command wasn't applied, because the coach isn't set up yet. "
    "Add your token with /plugin → promptcoach → Configure, then send it again."
)


@pytest.mark.parametrize("kind", sorted(NON_BLOCKING))
def test_command_with_a_non_blocking_reply_is_blocked(
    kind: str, server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    """The server's own message is the reason when it has one, so it isn't hidden."""
    server.reply = NON_BLOCKING[kind]

    out = json.loads(run_hook("before", prompt_payload(repo, "coach, quieter"), env))

    assert out == {"decision": "block", "reason": server.reply["message"] or REASON}
    # A real reply: its setting is still remembered. A blocked prompt keeps no snapshot.
    state = json.loads((data_dir / "state.json").read_text())
    assert state["prompt_only"] is server.reply["prompt_only"]
    assert not list(data_dir.glob("snap-*"))


@pytest.mark.parametrize("kind", sorted(NON_BLOCKING))
def test_ordinary_prompt_with_a_non_blocking_reply_is_unchanged(
    kind: str, server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = NON_BLOCKING[kind]
    message = server.reply["message"]

    out = run_hook("before", prompt_payload(project, "add a test"), env)

    assert out == (json.dumps({"systemMessage": message}) if message else "")


@pytest.mark.parametrize("prompt", COMMAND_PROMPTS)
def test_command_without_a_token_is_blocked(
    prompt: str, server: StubServer, env: dict[str, str], project: Path
) -> None:
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]

    out = run_hook("before", prompt_payload(project, prompt), env)

    assert json.loads(out) == {"decision": "block", "reason": NO_TOKEN_REASON}
    assert " ".join(prompt.split()).lower() not in out.lower()
    assert server.requests == []  # no token, no network


def test_command_without_a_token_leaves_the_setup_warning_for_later(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    """The block isn't the once-a-session warning: the next prompt still gets that."""
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]

    run_hook("before", prompt_payload(project, "coach, quieter"), env)
    out = run_hook("before", prompt_payload(project, "add a test", prompt_id="prompt-2"), env)

    assert json.loads(out) == {"systemMessage": PROFILE.missing_token_message}


def test_command_without_a_token_or_data_dir_is_blocked(env: dict[str, str], project: Path) -> None:
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]
    del env["CLAUDE_PLUGIN_DATA"]

    out = json.loads(run_hook("before", prompt_payload(project, "coach, quieter"), env))

    assert out == {"decision": "block", "reason": NO_TOKEN_REASON}


@pytest.mark.parametrize("prompt", ORDINARY_PROMPTS)
def test_ordinary_prompt_without_a_token_is_unchanged(
    prompt: str, env: dict[str, str], project: Path
) -> None:
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]

    out = run_hook("before", prompt_payload(project, prompt), env)

    assert json.loads(out) == {"systemMessage": PROFILE.missing_token_message}


@pytest.mark.parametrize("kind", sorted(NON_BLOCKING))
def test_no_block_for_an_agent_that_cannot_block_whatever_the_reply(
    kind: str, server: StubServer, env: dict[str, str], project: Path
) -> None:
    server.reply = NON_BLOCKING[kind]
    profile = dataclasses.replace(
        PROFILE, capabilities=core.Capabilities(False, has_reply=True, has_exit_codes=True)
    )
    event = core.PromptSubmitted("sess-1", "prompt-1", "coach, quieter", project, project)

    reply = core.before(event, claude_code.config_from_env(env), profile)

    assert reply is None or not reply.block


def test_no_token_block_needs_an_agent_that_can_block(env: dict[str, str], project: Path) -> None:
    del env["CLAUDE_PLUGIN_OPTION_TOKEN"]
    profile = dataclasses.replace(
        PROFILE, capabilities=core.Capabilities(False, has_reply=True, has_exit_codes=True)
    )
    event = core.PromptSubmitted("sess-1", "prompt-1", "coach, quieter", project, project)

    reply = core.before(event, claude_code.config_from_env(env), profile)

    assert reply == core.Reply(PROFILE.missing_token_message, False, True)


def test_every_request_names_the_plugin_version(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    from promptcoach_client import __version__

    run_hook("before", prompt_payload(project), env)
    run_hook("after", stop_payload(project), env)

    assert [r.path for r in server.requests] == ["/v1/before", "/v1/after"]
    for req in server.requests:
        assert req.headers["X-Promptcoach-Plugin"] == __version__
        assert [k for k, v in req.headers.items() if __version__ in v] == ["X-Promptcoach-Plugin"]
