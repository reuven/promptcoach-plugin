"""Fix round 4 (re-review 3): a blank run before a value, SIGTERM inside git's
start and at exit, escaped and letter-glued tokens, comparisons and more
command-line forms, interpreter-only script checks, and steadier timing tests.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import StubServer, prompt_payload
from promptcoach_client import gitdiff
from promptcoach_client.redact import prints_secrets, redact

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "promptcoach" / "hooks" / "coach_hook.py"
S = "Q7v9Zx3Lp0Wm2Rt8Yb6N"  # 20 made-up token characters

# --- M1: a run of blanks before the value ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("password =" + " " * 300 + '"padafter300"', "padafter300"),
        ("password:" + " " * 300 + "padafter300colon", "padafter300colon"),
    ],
)
def test_a_value_after_a_long_blank_run_is_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)


# --- M3: escaped and letter-glued tokens ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"a":"x\\nsk-' + S + '"}', S),
        ('{"a":"x\\tsk_live_' + S + '"}', S),
        ('"\\rhf_' + S + '"', S),
        ("xsk-proj-" + S, S),
        ("ask-svcacct-" + S, S),
        ("xsk-admin-" + S, S),
        ("AKIAABCDEFGHIJKLMNOP1", "ABCDEFGHIJKLMNOP"),
        ("ASIAABCDEFGHIJKLMNOPQRS", "ABCDEFGHIJKLMNOP"),
    ],
)
def test_escaped_and_glued_tokens_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)


# --- M4: comparisons and command-line forms -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('if password != "admin123x":', "admin123x"),
        ('if (secret !== "s3cr3tvalue") {', "s3cr3tvalue"),
        ("WHERE password <> 'admin123q'", "admin123q"),
        ('if "admin1234" == password:', "admin1234"),
        ("if 'admin5678' != self.password:", "admin5678"),
        ('if ("adminjs99" === apiKey) {', "adminjs99"),
        ("if password == ADMIN1PASS:", "ADMIN1PASS"),
        ("deploy --pass Sup3rS3cret", "Sup3rS3cret"),
        ("deploy --db-password Sup3rS3cret", "Sup3rS3cret"),
        ("app --client-secret=Sup3rS3cret", "Sup3rS3cret"),
        ("curl -uadmin:Sup3rS3cret https://x", "Sup3rS3cret"),
        ("curl --user=admin:Sup3rS3cret https://x", "Sup3rS3cret"),
        ("sshpass -p Sup3rS3cret ssh h", "Sup3rS3cret"),
        ("sshpass -pSup3rS3cret ssh h", "Sup3rS3cret"),
        ("htpasswd -b f admin Sup3rS3cret", "Sup3rS3cret"),
        ("htpasswd -cb .htpasswd admin Sup3rS3cret", "Sup3rS3cret"),
        # A comparison is kept only when its whole value is a dotted name.
        ('password == token.OP + "hunter2"', "hunter2"),
        ('password != 1234 or "s3cret"', "s3cret"),
        ("if password == 1234:", "1234"),
        ("if pin_secret != 0x5F3A:", "0x5F3A"),
        ("if token == 0x00:", "0x00"),
        # M6: a call taking a quoted UPPER name is kept only for an environment lookup
        ('password = unlock("HUNTERTWOPASS")', "HUNTERTWOPASS"),
    ],
)
def test_comparisons_and_more_forms_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "if token.start == token.end:",
        "and not (prev_token and prev_token.exact_type == T.DOT)",
        "elif start == dollar_end and token_type == token.NAME:",
        "if max_tokens != 1024:",
        "while token_type <> tokenize.NAME",
        "if password != self.password:",
        "deploy --passphrase-file key.txt",
        "curl --user admin https://x",
        "htpasswd -D f admin",
    ],
)
def test_comparisons_with_names_and_numbers_stay(text: str) -> None:
    assert redact(text) == text


# --- M5: inline scripts only for interpreters; more commands that print secrets ------------


@pytest.mark.parametrize(
    "command",
    [
        "node -p 'process.env.OPENAI_API_KEY'",
        "node -p process.env",
        "node -e 'console.log(process.env)'",
        "php -r 'echo getenv(\"OPENAI_API_KEY\");'",
        "php -r 'print_r(getenv());'",
        "perl -le 'print for %ENV'",
        "perl -e 'print $ENV{OPENAI_API_KEY}'",
        'python -c \'import os;print(os.getenv("OPENAI_" + "API_KEY"))\'',
        "python3 -c 'import os;k=\"ANTHROPIC_API_KEY\";print(os.getenv(k))'",
        "/usr/bin/python3 -I -c 'import os;print(os.getenv(\"GITHUB_TOKEN\"))'",
        "deno eval 'console.log(Deno.env.get(\"OPENAI_API_KEY\"))'",
        # A quoted secret-shaped name in an inline script, lookup or not.
        "python -c 'print(\"PASSWORD_RESET_TIMEOUT\")'",
        "python -c 'import mylib; mylib.read(\"GITHUB_TOKEN\")'",
        "ruby -e 'puts fetch_var(\"AWS_SECRET_ACCESS_KEY\")'",
        "node -e 'console.log(lookup(\"NPM_TOKEN\"))'",
        "bun -e 'console.log(process.env.OPENAI_API_KEY)'",
        "awk 'BEGIN{for(k in ENVIRON) print k, ENVIRON[k]}'",
        "aws configure get aws_secret_access_key",
        "vault read secret/data/x",
        "vault kv get secret/x",
        "kubectl config view --raw",
        "doppler secrets download --no-file --format env",
        "gh auth status --show-token",
        "gh auth status -t",
    ],
)
def test_round4_commands_that_print_secrets(command: str) -> None:
    assert prints_secrets(command)


@pytest.mark.parametrize(
    "command",
    [
        "grep -e 'SECRET_KEY' settings.py",
        'rg -e "GITHUB_TOKEN" .github/',
        "sed -e 's/API_TOKEN/X/' a.txt",
        "git -c core.editor=vim commit",
        "cat docs/operator-read.md",
        "heroku logs --tail",
        "gh auth status",
        "kubectl config view",
        "python -m pytest -k API_TOKEN",
    ],
)
def test_round4_false_positives_stay(command: str) -> None:
    assert not prints_secrets(command)


# --- M6: steadier timing --------------------------------------------------------------------


def _fastest(text: str) -> float:
    """The best of five timings of ``redact(text)``, the line tracer paused."""
    tracer = sys.gettrace()
    sys.settrace(None)
    try:
        times = []
        for _ in range(5):
            started = time.perf_counter()
            redact(text)
            times.append(time.perf_counter() - started)
    finally:
        sys.settrace(tracer)
    return min(times)


def test_empty_yaml_keys_take_linear_time() -> None:
    """Eight times the ``password:`` lines take about eight times as long, not
    sixty-four: a ratio, so a loaded machine slows both sides alike. Measured
    (probes/plugin/fix4/out/p34c_ratio8x.txt): linear code stays under 22 even
    with 48 CPU burners on 24 cores; the quadratic mutant is at 35 or more."""
    redact("password:\n" * 100)  # warm the caches
    small = _fastest("password:\n" * 5_000)
    large = _fastest("password:\n" * 40_000)
    assert large < 30 * small


SLOW_IMPORT = """
import importlib.abc, runpy, sys, time
class Slow(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "promptcoach_client.claude_code":
            time.sleep(1.0)
        return None
sys.meta_path.insert(0, Slow())
sys.argv = [sys.argv[1], "before"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_before_budget_counts_from_the_process_start(
    server: StubServer, env: dict[str, str], repo: Path
) -> None:
    """A second spent loading the client still counts against the 2.5 s."""
    server.mode, server.delay = "sleep", 10.0
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-I", "-c", SLOW_IMPORT, str(HOOK)],
        input=json.dumps(prompt_payload(repo)),
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
        timeout=10,
        check=False,
    )
    assert (done.returncode, done.stderr) == (0, "")
    assert time.monotonic() - started < 2.8


# --- M2: SIGTERM inside git's start, at exit, and in git itself -----------------------------

FAKE_GIT = "#!/bin/sh\necho $$ > {pid}\nexec sleep {seconds}\n"


def _fake_git(tmp_path: Path, seconds: str = "30") -> tuple[Path, Path]:
    """A ``git`` that records its pid and sleeps: (its bin directory, the pid file)."""
    pid_file = tmp_path / "git.pid"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text(FAKE_GIT.format(pid=pid_file, seconds=seconds))
    (fake / "git").chmod(0o755)
    return fake, pid_file


def _wait_for_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 10
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return int(pid_file.read_text())


def _gone(pid: int) -> bool:
    """True once ``pid`` is dead; kills it if not, so no test leaves it behind."""
    time.sleep(0.3)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    os.kill(pid, signal.SIGKILL)
    return False


def _run_child(script: str, tmp_path: Path) -> tuple[int, int]:
    """Run ``script`` with the fake git: (its exit status, how many of its gits
    are left running). Each git sleeps a time no other test uses, so it can be
    found by its command line even if it was killed before writing its pid."""
    seconds = f"30.{os.getpid()}{time.monotonic_ns() % 10**6}"
    fake, pid_file = _fake_git(tmp_path, seconds)
    child = subprocess.run(
        [sys.executable, "-I", "-c", script, str(HOOK.parent), str(tmp_path), str(pid_file)],
        env={"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"},
        timeout=20,
        check=False,
    )
    time.sleep(0.3)
    pattern = f"sleep {seconds}"
    left = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    subprocess.run(["pkill", "-9", "-f", pattern], check=False)
    return child.returncode, len(left.stdout.split())


CHILD_HEAD = """
import os, pathlib, signal, subprocess, sys, threading, time
sys.path.insert(0, sys.argv[1])
from promptcoach_client import gitdiff
gitdiff.install_cleanup()
pid_file = pathlib.Path(sys.argv[3])
"""

# git is running but not yet in _LIVE: Popen's return stretched, as on a loaded machine.
SLOW_START = """
real = subprocess.Popen
class SlowPopen(real):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        time.sleep(1.0)
gitdiff.subprocess.Popen = SlowPopen
threading.Thread(
    target=gitdiff.run_git, args=(["status"], pathlib.Path(sys.argv[2])),
    kwargs={"timeout": 30}, daemon=True,
).start()
while not pid_file.exists() or not pid_file.read_text().strip():
    time.sleep(0.02)
"""


def test_sigterm_while_another_thread_starts_git_kills_it(tmp_path: Path) -> None:
    """The handler waits for the starting thread (R10: no lock in the handler)."""
    script = CHILD_HEAD + SLOW_START + "os.kill(os.getpid(), signal.SIGTERM)\ntime.sleep(30)\n"
    code, orphans = _run_child(script, tmp_path)
    assert code == -signal.SIGTERM
    assert orphans == 0


def test_exit_while_another_thread_starts_git_kills_it(tmp_path: Path) -> None:
    """The exit cleanup waits for the starting thread too (p32)."""
    code, orphans = _run_child(CHILD_HEAD + SLOW_START, tmp_path)
    assert code == 0
    assert orphans == 0


# SIGTERM's handler runs right after run_git blocked the signal: CPython runs a
# Python-level handler late, so a signal that came just before the mask lands here.
EARLY_HANDLER = """
real = signal.pthread_sigmask
fired = []
def mask(how, sigs):
    old = real(how, sigs)
    if how == signal.SIG_BLOCK and sigs and not fired:
        fired.append(1)
        gitdiff._on_term(signal.SIGTERM, None)
    return old
signal.pthread_sigmask = mask
gitdiff.run_git(["status"], pathlib.Path(sys.argv[2]), timeout=30)
"""


def test_sigterm_handled_inside_the_start_window_kills_git(tmp_path: Path) -> None:
    """Deferred while git starts, then run: no orphan (p30)."""
    code, orphans = _run_child(CHILD_HEAD + EARLY_HANDLER, tmp_path)
    assert code == -signal.SIGTERM
    assert orphans == 0


def test_a_deferred_sigterm_runs_once_git_is_tracked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        gitdiff, "_PREVIOUS_TERM", lambda signum, frame: seen.append((signum, bool(gitdiff._LIVE)))
    )
    real = signal.pthread_sigmask
    deferred: list[int] = []

    def mask(how: int, sigs: object) -> set[int]:
        old = real(how, sigs)  # type: ignore[arg-type]
        if how == signal.SIG_BLOCK and sigs and not deferred:
            gitdiff._on_term(signal.SIGTERM, None)
            deferred.append(len(seen))
        return old

    killed: list[int] = []
    real_kill_live = gitdiff._kill_live

    def kill_live() -> None:
        killed.extend(gitdiff._LIVE)
        real_kill_live()

    monkeypatch.setattr(signal, "pthread_sigmask", mask)
    monkeypatch.setattr(gitdiff, "_kill_live", kill_live)
    gitdiff.run_git(["status"], repo)
    assert deferred == [0]  # nothing ran inside the window
    assert len(killed) == 1  # then git, tracked by now, was killed
    assert [s for s, _ in seen] == [signal.SIGTERM]
    assert not gitdiff._PENDING_TERM


def test_sigterm_during_the_exit_cleanup_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit cleanup holds the start lock; a SIGTERM handled in the same
    thread takes it again (R11: a plain Lock deadlocks)."""
    seen: list[int] = []
    monkeypatch.setattr(gitdiff, "_PREVIOUS_TERM", lambda signum, frame: seen.append(signum))
    monkeypatch.setattr(gitdiff, "_LIVE", {2**22 + 7})
    reentered: list[bool] = []

    def killpg(pgid: int, signum: int) -> None:
        if not reentered:
            reentered.append(True)
            gitdiff._on_term(signal.SIGTERM, None)

    monkeypatch.setattr(os, "killpg", killpg)
    cleanup = threading.Thread(target=gitdiff._exit_cleanup, daemon=True)
    cleanup.start()
    cleanup.join(3)
    stuck = cleanup.is_alive()
    if stuck:  # free the other tests: a plain Lock can be released from here
        with contextlib.suppress(RuntimeError):
            gitdiff._STARTING.release()
    assert not stuck
    assert seen == [signal.SIGTERM]


def test_git_does_not_inherit_a_blocked_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphaned git (the hook killed with SIGKILL) still obeys a later SIGTERM."""
    fake, pid_file = _fake_git(tmp_path)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    results: list[object] = []
    runner = threading.Thread(
        target=lambda: results.append(gitdiff.run_git(["status"], tmp_path, timeout=10)),
        daemon=True,
    )
    started = time.monotonic()
    runner.start()
    git_pid = _wait_for_pid(pid_file)
    os.kill(git_pid, signal.SIGTERM)
    runner.join(5)
    gone = _gone(git_pid)
    runner.join(10)
    assert gone
    assert time.monotonic() - started < 5
    assert results == [None]
