"""Shared fixtures: a stub coach server, a hook runner, and throwaway git repos."""

from __future__ import annotations

import _thread
import atexit
import contextlib
import faulthandler
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings

from promptcoach_client import gitdiff
from promptcoach_client.claude_code import main

# Hypothesis profiles: small for the red/green loop, large and derandomized on
# CI (the built-in ci profile, raised), tiny and derandomized under mutmut,
# where every mutant reruns the tests.
settings.register_profile("dev", max_examples=25)
settings.register_profile("ci", settings.get_profile("ci"), max_examples=200)
settings.register_profile("mutmut", max_examples=10, derandomize=True, deadline=None)


def hypothesis_profile(env: Mapping[str, str], cwd: Path) -> str:
    """Which profile to load: ``HYPOTHESIS_PROFILE`` if set, then mutmut (its
    runs start in a ``mutants/`` copy), then ci when ``CI`` is set, then dev.

    Why not just ``HYPOTHESIS_PROFILE`` with a dev default: that would replace
    the profile Hypothesis picks on CI, quietly running 25 examples there.
    """
    if env.get("HYPOTHESIS_PROFILE"):
        return env["HYPOTHESIS_PROFILE"]
    if cwd.name == "mutants":
        return "mutmut"
    return "ci" if env.get("CI") else "dev"


settings.load_profile(hypothesis_profile(os.environ, Path.cwd()))

# No test may run longer than this. A test that blocks (a FIFO opened without
# O_NONBLOCK, a pipe nobody closes) fails with a TimeoutError instead of
# hanging the run and holding a test slot. SIGALRM interrupts a blocking
# system call in the main thread; no plugin needed, and it works on 3.9.
TEST_TIMEOUT_S = 120
TIMED_OUT: list[bool] = []  # set when the alarm fires, even if something eats it


class TestTimedOut(BaseException):
    """A test ran too long. Not an Exception (nor an OSError, as TimeoutError
    is), so the hook's own ``except Exception`` can't swallow it."""

    __test__ = False


def _too_slow(signum: int, frame: object) -> None:
    TIMED_OUT.append(True)
    raise TestTimedOut(f"test ran longer than {TEST_TIMEOUT_S} s")


def fail_if_timed_out() -> None:
    """Fail the test if its alarm fired, whatever happened to the exception."""
    if TIMED_OUT:
        TIMED_OUT.clear()
        pytest.fail(f"test ran longer than {TEST_TIMEOUT_S} s")


@contextlib.contextmanager
def interrupt_after(seconds: float) -> Iterator[None]:
    """Raise ``TestTimedOut`` in the main thread after ``seconds``, without SIGALRM.

    Windows has no SIGALRM: a timer thread simulates a SIGINT instead, whose
    handler raises. It can't interrupt a blocking system call (SIGALRM on
    POSIX can), but it ends a test stuck in Python code.
    """
    previous = signal.signal(signal.SIGINT, _too_slow)
    timer = threading.Timer(seconds, _thread.interrupt_main)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        signal.signal(signal.SIGINT, previous)


# The backstop behind TEST_TIMEOUT_S. On Windows the per-test timer can't
# interrupt a blocking call (a pipe read, a wait), and nothing times a hang
# outside a test (collection, between tests, the session's end, or Python
# waiting at exit for a thread nobody stopped). So one faulthandler watchdog
# stays armed from configure to the process's exit, re-armed at each step:
# past HARD_LIMIT_S without one, it writes every thread's stack to the hang
# log and exits the run non-zero, long before CI's step timeout. Each step's
# name goes in first, so the log says where it hung. A raw file descriptor,
# not stderr (pytest captures it, and exiting loses it) and not a file
# object (it must stay open, unflushed writes and all, until the very end).
HARD_LIMIT_S = float(os.environ.get("PROMPTCOACH_TEST_HARD_LIMIT_S", TEST_TIMEOUT_S + 30))
HANG_LOG = Path(
    os.environ.get("PROMPTCOACH_HANG_LOG")
    or Path(tempfile.gettempdir()) / f"promptcoach-pytest-hang-{os.getpid()}.log"
)
_HANG_FD: list[int] = []


def _arm(step: str) -> None:
    """Note ``step`` in the hang log and restart the watchdog's clock."""
    if not _HANG_FD:
        _HANG_FD.append(os.open(HANG_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644))
        atexit.register(_disarm)
    os.write(_HANG_FD[0], f"{step}\n".encode())
    faulthandler.dump_traceback_later(HARD_LIMIT_S, exit=True, file=_HANG_FD[0])


def _disarm() -> None:
    """At exit, after Python has joined every thread: no hang, so stop the
    watchdog and drop the log unless CI asked for it."""
    faulthandler.cancel_dump_traceback_later()
    for fd in _HANG_FD:
        os.close(fd)
    _HANG_FD.clear()
    if not os.environ.get("PROMPTCOACH_HANG_LOG"):
        with contextlib.suppress(OSError):
            HANG_LOG.unlink()


def pytest_configure(config: pytest.Config) -> None:
    _arm("collecting")


def pytest_sessionfinish(session: pytest.Session) -> None:
    _arm("session finishing")


def pytest_unconfigure(config: pytest.Config) -> None:
    _arm("exiting")


@pytest.fixture(autouse=True)
def _hard_limit(request: pytest.FixtureRequest) -> Iterator[None]:
    _arm(f"running {request.node.nodeid}")
    try:
        yield
    finally:
        _arm(f"after {request.node.nodeid}")


@pytest.fixture(autouse=True)
def _sigterm_left_alone() -> Iterator[None]:
    """After each test, SIGTERM is unblocked in the main thread and its
    handler is the one the test found: a test that leaks either would make
    the whole run deaf to CI's stop signal.

    A hook run in-process installs its exit cleanup (a SIGTERM handler and an
    atexit call) once per process, as it should in a real hook. In pytest
    that leaked for the whole session, so every later SIGTERM, CI's stop
    signal included, went through the hook's handler. It is undone here,
    before the check, so only a leak of any other kind fails a test.
    """
    before = signal.getsignal(signal.SIGTERM)
    yield
    if gitdiff._CLEANUP_INSTALLED:
        gitdiff._CLEANUP_INSTALLED.clear()
        atexit.unregister(gitdiff._exit_cleanup)
        previous = gitdiff._PREVIOUS_TERM
        signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)
    if hasattr(signal, "pthread_sigmask"):
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        assert signal.SIGTERM not in blocked, "the test left SIGTERM blocked"
    assert signal.getsignal(signal.SIGTERM) == before, "the test left a SIGTERM handler"


@pytest.fixture(autouse=True)
def _per_test_timeout() -> Iterator[None]:
    TIMED_OUT.clear()
    if not hasattr(signal, "SIGALRM"):  # Windows
        with interrupt_after(TEST_TIMEOUT_S):
            yield
        fail_if_timed_out()
        return
    previous = signal.signal(signal.SIGALRM, _too_slow)
    signal.alarm(TEST_TIMEOUT_S)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    fail_if_timed_out()


def posix_only(windows_test: str, why: str) -> pytest.MarkDecorator:
    """Skip on Windows, naming the test that covers the same behavior there.

    Never a silent skip: the reason says what is POSIX-only and where the
    Windows side is tested.
    """
    return pytest.mark.skipif(
        os.name == "nt", reason=f"POSIX only ({why}); on Windows, {windows_test} covers it"
    )


TOKEN = "pc_" + "t" * 43  # the server's token shape; a dummy
SILENT = {"message": None, "block_prompt": False, "prompt_only": False}

# Prompts the plugin reads as coach commands, and near misses it must not.
# Shared by the block tests and the fail-closed tests, so the detector's
# edge cases (spacing, case, curly apostrophes, length) are stated once.
COMMAND_PROMPTS = [
    "coach, quieter",
    "Coach: status?",
    "coach be louder please",
    "  coach,   prompt-only on.  ",
    "coach, that wasn\u2019t helpful",
    "COACH learning off",
    "coach, thanks!",
]
ORDINARY_PROMPTS = [
    "add a test",
    "coach, helpful?",
    "coach, that helped. Now add tests",
    "coach, be helpful",
    "coach, quieter\nand then fix the bug",
    "coach, thank\u017f",
    "coach, quieter" + " " * 1_000,  # a command is short; long prompts aren't read
]


@dataclass
class Received:
    """One request the stub server got."""

    path: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class StubServer:
    """A local coach server whose next answers a test chooses.

    ``mode`` is one of ``json`` (send ``reply``), ``status`` (send
    ``status`` with an empty body), ``raw`` (send ``raw`` as the body),
    ``sleep`` (wait up to ``delay`` s, then answer), ``redirect`` or
    ``badstatus`` (a reply that isn't HTTP).
    """

    url: str
    requests: list[Received] = field(default_factory=list)
    mode: str = "json"
    reply: dict[str, Any] = field(default_factory=lambda: dict(SILENT))
    status: int = 200
    raw: bytes = b""
    delay: float = 5.0  # how long ``sleep`` waits
    redirect_to: str = ""  # where ``redirect`` points (default: this server)
    redirect_status: int = 307
    release: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def server() -> Iterator[StubServer]:
    """A coach server stub on 127.0.0.1, stopped (and unblocked) after the test."""
    yield from _stub_server()


@pytest.fixture
def other_server() -> Iterator[StubServer]:
    """A second stub, on another port: somewhere a redirect might point."""
    yield from _stub_server()


def _stub_server() -> Iterator[StubServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    httpd.daemon_threads = True
    stub = StubServer(url=f"http://127.0.0.1:{httpd.server_address[1]}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # only a followed redirect would ever GET
            stub.requests.append(Received(self.path, dict(self.headers), {}))
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            stub.requests.append(Received(self.path, dict(self.headers), body))
            if stub.mode == "sleep":
                stub.release.wait(stub.delay)
            if stub.mode == "badstatus":
                self.wfile.write(b"GARBAGE\r\n\r\n")  # not HTTP: BadStatusLine
                return
            if stub.mode == "trickle":
                # Headers, then one byte at a time: no socket read ever times out.
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                for _ in range(10):
                    if stub.release.wait(0.5):
                        return
                    self.wfile.write(b" ")
                    self.wfile.flush()
                return
            if stub.mode == "redirect":
                self.send_response(stub.redirect_status)
                self.send_header("Location", stub.redirect_to or stub.url + "/elsewhere")
                self.end_headers()
                return
            payload = {
                "json": json.dumps(stub.reply).encode(),
                "raw": stub.raw,
                "sleep": json.dumps(stub.reply).encode(),
            }.get(stub.mode, b"")
            self.send_response(stub.status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd.RequestHandlerClass = Handler
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield stub
    stub.release.set()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An empty home directory, so the real one is never read."""
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An empty project directory (not a git repo unless a test makes it one)."""
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """The plugin's data directory (``CLAUDE_PLUGIN_DATA``)."""
    return tmp_path / "plugin-data"


@pytest.fixture
def env(server: StubServer, home: Path, project: Path, data_dir: Path) -> dict[str, str]:
    """A hook environment pointing at the stub server, with a token set."""
    return {
        "CLAUDE_PLUGIN_OPTION_TOKEN": TOKEN,
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": server.url,
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "CLAUDE_PROJECT_DIR": str(project),
        "HOME": str(home),
    }


# What a child Python needs from the environment beyond PATH. On Windows,
# without SYSTEMROOT OpenSSL can't make a context (ssl.SSLError at import of
# urllib's HTTPS handler) and other system calls fail; Claude Code passes its
# whole environment, so a test child must have these too.
_WINDOWS_VARS = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def child_env(env: Mapping[str, str] | None = None, path: str | None = None) -> dict[str, str]:
    """A small environment for a hook started as a child: PATH (or ``path``),
    the variables Windows needs, then ``env``."""
    base = {"PATH": os.environ["PATH"] if path is None else path}
    base.update({k: os.environ[k] for k in _WINDOWS_VARS if k in os.environ})
    return {**base, **(env or {})}


def run_hook(mode: str, payload: object, env: dict[str, str]) -> str:
    """Run the hook in-process, as Claude Code would; return what it printed."""
    stdout = io.StringIO()
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    assert main([mode], stdin, stdout, env) == 0
    return stdout.getvalue()


def prompt_payload(project: Path, prompt: str = "add a test", **extra: object) -> dict[str, object]:
    """A ``UserPromptSubmit`` payload, shaped as verified on Claude Code 2.1.326."""
    return {
        "session_id": "sess-1",
        "prompt_id": "prompt-1",
        "prompt": prompt,
        "cwd": str(project),
        "transcript_path": "/dev/null",
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        **extra,
    }


def stop_payload(project: Path, **extra: object) -> dict[str, object]:
    """A ``Stop`` payload."""
    return {
        "session_id": "sess-1",
        "prompt_id": "prompt-1",
        "cwd": str(project),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Done: added the test.",
        **extra,
    }


def bash_payload(project: Path, command: str, **extra: object) -> dict[str, object]:
    """A successful Bash ``PostToolUse`` payload."""
    return {
        "session_id": "sess-1",
        "prompt_id": "prompt-1",
        "cwd": str(project),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "run it"},
        "tool_response": {
            "stdout": "out",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
        **extra,
    }


def failure_payload(project: Path, command: str, error: str, **extra: object) -> dict[str, object]:
    """A failed Bash ``PostToolUseFailure`` payload (no ``tool_response``)."""
    return {
        "session_id": "sess-1",
        "prompt_id": "prompt-1",
        "cwd": str(project),
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "run it"},
        "error": error,
        "is_interrupt": False,
        **extra,
    }


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a fixed identity; its stdout."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(project: Path) -> Path:
    """``project`` as a git repo with one commit."""
    git(project, "init", "-q")
    (project / "app.py").write_text("print('v1')\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    return project
