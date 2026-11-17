"""Shared fixtures: a stub coach server, a hook runner, and throwaway git repos."""

from __future__ import annotations

import io
import json
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from promptcoach_client.claude_code import main

TOKEN = "pc_" + "t" * 43  # the server's token shape; a dummy
SILENT = {"message": None, "block_prompt": False, "prompt_only": False}


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
    ``sleep`` (wait up to 5 s, then answer) or ``redirect``.
    """

    url: str
    requests: list[Received] = field(default_factory=list)
    mode: str = "json"
    reply: dict[str, Any] = field(default_factory=lambda: dict(SILENT))
    status: int = 200
    raw: bytes = b""
    release: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def server() -> Iterator[StubServer]:
    """A coach server stub on 127.0.0.1, stopped (and unblocked) after the test."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    httpd.daemon_threads = True
    stub = StubServer(url=f"http://127.0.0.1:{httpd.server_address[1]}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            stub.requests.append(Received(self.path, dict(self.headers), body))
            if stub.mode == "sleep":
                stub.release.wait(5)
            if stub.mode == "redirect":
                self.send_response(307)
                self.send_header("Location", stub.url + "/elsewhere")
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
