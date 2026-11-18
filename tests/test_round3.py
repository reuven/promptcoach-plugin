"""Fix round 3 (re-review 2): walrus assignments and glued tokens, the test
timeout escaping hook-style handlers, calls with literal arguments and other
value forms, a reply cut inside a key, YAML speed, the before-hook clock,
more secret-printing commands, and SIGTERM around git's start.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import conftest
from conftest import StubServer, posix_only, prompt_payload
from promptcoach_client import core, gitdiff
from promptcoach_client.contract import MAX_REPLY_CHARS
from promptcoach_client.redact import prints_secrets, redact

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "promptcoach" / "hooks" / "coach_hook.py"
KEY = "sk-ant-api03-" + "Zq7" * 30

# --- I-1: := assigns; glued tokens ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('password := "hunter2"', "hunter2"),
        ('(token := "ghx-abc123secret")', "ghx-abc123secret"),
        ("echo 'line\\n" + KEY + "'", "Zq7Zq7Zq7Zq7"),
        ("x_ghp_" + "a1" * 15, "a1a1a1a1a1"),
        ("1AKIAABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
        ("q" * 50 + KEY, "Zq7Zq7Zq7Zq7"),
        ("id=7sk-abcdefghijklmnopqr", "abcdefghijklmnopqr"),
        ("val=\\tglpat-" + "x" * 20, "xxxxxxxxxxxxxxxxxxxx"),
    ],
)
def test_walrus_and_glued_tokens_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)


@pytest.mark.parametrize(
    "text",
    ["task-management-system-v2", "a disk-backed-store-name", "Bearer tokens are documented"],
)
def test_words_ending_in_sk_stay(text: str) -> None:
    assert redact(text) == text


# --- I-2: a hang inside a hook-style handler fails the test --------------------------------


@posix_only("test_windows.py::test_the_per_test_timeout_works_without_sigalarm", "setitimer")
def test_timeout_escapes_hook_style_handlers() -> None:
    """The test timeout isn't an OSError or an Exception: ``except`` can't eat it."""
    assert not issubclass(conftest.TestTimedOut, Exception)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    try:
        with pytest.raises(conftest.TestTimedOut), contextlib.suppress(OSError, Exception):
            time.sleep(5)  # inside a handler like the hook's
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        conftest.TIMED_OUT.clear()


def test_a_swallowed_timeout_still_fails_the_test() -> None:
    conftest.TIMED_OUT.append(True)
    try:
        with pytest.raises(pytest.fail.Exception):
            conftest.fail_if_timed_out()
    finally:
        conftest.TIMED_OUT.clear()
    conftest.fail_if_timed_out()  # nothing recorded: no failure


# --- M-1: literal arguments, long lines, comparisons, CLI and markup ----------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('totp_secret = pyotp.TOTP("JBSWY3DPEHPK3PXP")', "JBSWY3DPEHPK3PXP"),
        ('api_key = decrypt("ABCDEFGH12345678WXYZ")', "ABCDEFGH12345678WXYZ"),
        ("secret = Fernet(KEY_9F8E7D6C5B4A)", "KEY_9F8E7D6C5B4A"),
        ("password = check(84736251)", "84736251"),
        ("password = getpass()" + " " * 300 + '"hunter2"', "hunter2"),
        ('if password == "admin123":', "admin123"),
        ("if token == other_token:", "other_token"),
        ("mysql --password hunter2 db", "hunter2"),
        ("tool --api-key=hunter2 x", "hunter2"),
        ("tool --token hunter2", "hunter2"),
        ("tool --secret hunter2", "hunter2"),
        ("tool --passwd hunter2", "hunter2"),
        ("mysql -u root -phunter2 db", "hunter2"),
        ("docker login -u me -p hunter2 registry", "hunter2"),
        ("curl -u admin:hunter2 https://x", "hunter2"),
        ("<password>hunter2</password>", "hunter2"),
        ("<db:Password>hunter2</db:Password>", "hunter2"),
        ('<add key="DbPassword" value="hunter2"/>', "hunter2"),
        ('client.login("admin", "hunter2")', "hunter2"),
        ("secret_key_base: 8a1f9c3e7b2d4f60a9e8c7b6d5f4e3a2", "8a1f9c3e"),
        ('api_key = os.getenv("X", "fallback-secret")', "fallback-secret"),
        ("token = make_token(user.id, size=32),", "size=32"),
    ],
)
def test_more_value_forms_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens = 4096  # the reply's cap",
        "if password_hash == compute(pw):",
        "password = get_password()",
        'token = os.environ["TOKEN"]',
        'api_key = os.getenv("X", None)',
        'key = os.environ.get("API_KEY")',
        'key = environ.get("API_KEY")',
        'key = getenv("API_KEY")',
        "secret = client.secrets.get(name)",
        "password=password",
        "mysql -u root -p",
        "curl -u admin https://x",
        "<password/>",
    ],
)
def test_code_and_prompts_stay(text: str) -> None:
    assert redact(text) == text


# --- M-2: the reply is redacted before it is cut ------------------------------------------


def test_reply_cut_inside_a_key_is_redacted(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    """The reviewer's p27: a key straddling the reply's 20,000-char cap."""
    data_dir.mkdir(exist_ok=True)
    (data_dir / "state.json").write_text('{"prompt_only": false}')
    reply = "x" * 30 + KEY + " " + "y" * (MAX_REPLY_CHARS - 60)
    conftest.run_hook("after", conftest.stop_payload(project, last_assistant_message=reply), env)
    sent = server.requests[-1].body["last_assistant_message"]
    assert sent is not None and sent.endswith("y" * 100)
    assert "Zq7Zq7Zq7Zq7" not in sent


# --- M-3: YAML next-line values stay linear -----------------------------------------------


# --- M-4: the before hook's clock starts when the hook starts ------------------------------


def test_before_deadline_counts_from_the_hook_start(
    server: StubServer, env: dict[str, str], repo: Path
) -> None:
    server.mode, server.delay = "sleep", 10.0
    started = time.monotonic() - 1.0  # a second already spent before core.before runs
    event = core.PromptSubmitted("sess-1", "prompt-1", "fix it", repo, repo)
    config = core.Config("tok", server.url, False, None, Path(env["HOME"]))
    from promptcoach_client.claude_code import PROFILE

    core.before(event, config, PROFILE, started=started)
    assert time.monotonic() - started < core.BEFORE_BUDGET_S + 0.1


def test_before_hook_finishes_under_its_limit(
    server: StubServer, env: dict[str, str], repo: Path
) -> None:
    """A slow server and git: the whole process, start to exit, under 2.7 s."""
    server.mode, server.delay = "sleep", 10.0
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-I", str(HOOK), "before"],
        input=json.dumps(prompt_payload(repo)),
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
        timeout=10,
        check=False,
    )
    assert done.returncode == 0
    assert time.monotonic() - started < 2.7


# --- M-5: more commands that print secrets -------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'import os;print(os.getenv(\"OPENAI_API_KEY\"))'",
        "ruby -e 'p ENV'",
        "cat /proc/self/environ",
        "cat /proc/1234/environ",
        "op read op://vault/item/password",
        "security find-generic-password -w -s svc",
        "security find-internet-password -w -s svc",
        "sops -d secrets.enc.yaml",
        "sops --decrypt secrets.enc.yaml",
        "git credential fill",
        "gcloud auth print-access-token",
        "aws sts get-session-token",
        "heroku config",
    ],
)
def test_round3_commands_that_print_secrets(command: str) -> None:
    assert prints_secrets(command)


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/test_token.py",
        "cat README.md",
        "python manage.py runserver",
        "make test",
        "echo $PATH",
        "ls -la",
        "git status",
        "env PYTHONPATH=. python app.py",
        "uv run pytest -k password_reset",
        "cat tests/test_auth_token.py",
        "grep -n TOKEN src/app.py",
        "python -c 'print(\"HELLO\")'",
        'grep -r "API_KEY" .',
    ],
)
def test_reviewers_false_positives_stay(command: str) -> None:
    assert not prints_secrets(command)


# --- M-6: SIGTERM can't land between git's start and its being tracked --------------------


@posix_only("test_windows.py::test_windows_git_runs_without_a_group_or_mask", "signal masks")
def test_sigterm_is_blocked_while_git_starts(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    masks: list[set[int]] = []
    real = subprocess.Popen

    def watching(args: list[str], **kwargs: object) -> object:
        masks.append(set(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
        return real(args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "Popen", watching)
    assert gitdiff.run_git(["status"], repo) is not None
    assert masks and all(signal.SIGTERM in mask for mask in masks)
    assert signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, [])
