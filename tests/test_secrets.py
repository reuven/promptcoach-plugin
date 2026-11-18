"""Fix round 1, I3 and M4: secrets in command output, the agent's reply and the
diff; and strings that aren't valid UTF-8.

The client doesn't port the server's scrubber. It blanks the output of
commands that plainly print credentials, and redacts well-known token shapes
and ``NAME=value`` lines whose name says secret. The server scrubs again.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import (
    StubServer,
    bash_payload,
    git,
    prompt_payload,
    run_hook,
    stop_payload,
)
from promptcoach_client.redact import prints_secrets, redact

FAKE = "sk-fake-ENVFILE-0001"


def _not_prompt_only(data_dir: Path) -> None:
    data_dir.mkdir(exist_ok=True)
    (data_dir / "state.json").write_text('{"prompt_only": false}')


def _bodies(server: StubServer) -> str:
    return json.dumps([r.body for r in server.requests])


# --- (a) commands that print credentials: output blanked, run kept ---------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "less config/.env.local",
        "source .env && pytest",
        ". ./.env",
        "python app.py < .env",
        "python app.py <.env",
        "head -5 ~/.ssh/id_rsa",
        "grep KEY '.env'",
        "printenv",
        "env",
        "/usr/bin/env | sort",
        "sudo env",
        "set",
        "export -p",
        "declare -x",
        "ls; printenv HOME",
    ],
)
def test_commands_that_print_secrets(command: str) -> None:
    assert prints_secrets(command)


@pytest.mark.parametrize(
    "command",
    ["pytest -q", "cat app.py", "ls -la", "cat .env.example", "export PATH=$PATH:/x", "git status"],
)
def test_ordinary_commands(command: str) -> None:
    assert not prints_secrets(command)


def test_secret_printing_run_keeps_exit_code_but_no_output(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _not_prompt_only(data_dir)
    run_hook("before", prompt_payload(repo), env)
    payload = bash_payload(repo, "cat .env")
    payload["tool_response"] = {"stdout": f"FAKE_SECRET={FAKE}", "stderr": ""}
    run_hook("record", payload, env)

    assert FAKE not in (data_dir / "runs-sess-1").read_text()  # never even written
    run_hook("after", stop_payload(repo), env)
    runs = server.requests[-1].body["tool_runs"]
    assert runs == [{"command": "cat .env", "exit_code": 0, "output_tail": "", "is_test": False}]


# --- (b) the redactor ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        FAKE,
        "sk-ant-api03-abcdefghijklmnopqrstuv",
        "sk-proj-AbCdEfGh12345678",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "gho_abcdefghijklmnopqrstuvwxyz0123",
        "ghs_abcdefghijklmnopqrstuvwxyz0123",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "xoxb-1234567890-abcdefghij",
        "xoxp-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "AIzaSyA-abcdefghijklmnopqrstuvwxyz0123",
        "glpat-abcdefghij0123456789",
    ],
)
def test_token_shapes_are_redacted(secret: str) -> None:
    out = redact(f"before {secret} after\nnext line")
    assert secret not in out
    assert "[REDACTED]" in out
    assert out.startswith("before ") and out.endswith("after\nnext line")


@pytest.mark.parametrize(
    ("line", "kept", "value"),
    [
        ("DB_PASSWORD=hunter2", "DB_PASSWORD=", "hunter2"),
        ("export GITHUB_TOKEN=abc123", "export GITHUB_TOKEN=", "abc123"),
        ("client_secret: s3cr3t-value", "client_secret:", "s3cr3t"),
        ('"api_key": "abc123",', '"api_key":', "abc123"),
        ("OPENAI_API_KEY = 'abc123'", "OPENAI_API_KEY =", "abc123"),
        ("Private_Key='xyz'", "Private_Key=", "xyz"),
    ],
)
def test_secret_named_assignments_are_redacted(line: str, kept: str, value: str) -> None:
    out = redact(f"x = 1\n{line}\ny = 2")
    assert value not in out
    assert out.startswith(f"x = 1\n{kept}")
    assert "[REDACTED]" in out
    assert out.endswith("\ny = 2")


def test_private_key_blocks_are_redacted() -> None:
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAAA\n-----END RSA PRIVATE KEY-----"
    out = redact(f"start\n{block}\nend")
    assert "MIIEow" not in out and "AAAA" not in out
    assert out.startswith("start\n") and out.endswith("\nend")


def test_unterminated_private_key_is_redacted_to_the_end() -> None:
    out = redact("start\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz\nmore")
    assert "b3BlbnNz" not in out and "more" not in out


@pytest.mark.parametrize(
    "text",
    [
        "print('hello')",
        "def test_login(): ...",
        "tokens are cheap",
        "task-runner-01",
        "",
        "PASSWORD=\nnext",  # nothing after the name on its line
    ],
)
def test_ordinary_text_is_kept(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "a" * 1_000_000,
        "TOKEN" * 200_000,
        "sk-" * 333_333,
        "-----BEGIN PRIVATE KEY-----" * 37_000,
        "password_token_secret " * 45_000,
        ("x" * 999 + "\n") * 1_000,
        "AKIA" * 250_000,
    ],
    # The 1 MB strings must not be their own ids (see test_every_test_id_is_short).
    ids=["a", "TOKEN", "sk-", "pem-header", "keywords", "x-lines", "AKIA"],
)
def test_redaction_is_linear(text: str) -> None:
    """1 MB in well under a second: a quadratic pattern would take minutes.

    The fix-round brief asks for 0.2 s; the suite has no timing marker, so
    the bound here is loose (1 s) to stay reliable on a slow CI machine.
    """
    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 1.0


def test_output_reply_and_diff_are_redacted_end_to_end(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _not_prompt_only(data_dir)
    run_hook("before", prompt_payload(repo), env)
    payload = bash_payload(repo, "python app.py")
    payload["tool_response"] = {"stdout": f"connecting with {FAKE}\nAPI_KEY=abc123", "stderr": ""}
    run_hook("record", payload, env)
    (repo / "app.py").write_text(f"KEY = '{FAKE}'\nDB_PASSWORD = 'hunter2'\n")

    run_hook("after", stop_payload(repo, last_assistant_message=f"Your key is {FAKE}"), env)

    body = server.requests[-1].body
    sent = _bodies(server)
    assert FAKE not in sent and "hunter2" not in sent and "abc123" not in sent
    assert "[REDACTED]" in body["diff"]
    assert "[REDACTED]" in body["last_assistant_message"]
    assert "[REDACTED]" in body["tool_runs"][0]["output_tail"]


def test_fallback_diff_is_redacted(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _not_prompt_only(data_dir)
    (repo / "app.py").write_text(f"KEY = '{FAKE}'\n")
    git(repo, "status")

    run_hook("after", stop_payload(repo), env)

    assert FAKE not in _bodies(server)


# --- M4: every string sent is valid UTF-8 ----------------------------------------------


def test_lone_surrogates_are_replaced(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _not_prompt_only(data_dir)
    run_hook("before", prompt_payload(repo, prompt="fix \ud83d it"), env)
    payload = bash_payload(repo, "pytest \udc00")
    payload["tool_response"] = {"stdout": "out \ud83d", "stderr": ""}
    run_hook("record", payload, env)
    run_hook("after", stop_payload(repo, last_assistant_message="done \udfff"), env)

    for request in server.requests:
        raw = json.dumps(request.body, ensure_ascii=False)
        raw.encode("utf-8")  # raises on a lone surrogate
    assert server.requests[0].body["prompt"] == "fix \ufffd it"
    after = server.requests[-1].body
    assert after["last_assistant_message"] == "done \ufffd"
    assert after["tool_runs"][0]["output_tail"] == "out \ufffd"
    assert after["tool_runs"][0]["command"] == "pytest \ufffd"
