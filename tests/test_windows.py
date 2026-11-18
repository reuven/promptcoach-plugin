"""Windows: the git path, text encodings, CRLF and Windows paths.

The Windows branch of ``gitdiff`` is chosen by ``gitdiff.WINDOWS``; these
tests set it, so both branches run (and count for coverage) on every OS.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import coach_hook
from conftest import StubServer, prompt_payload, run_hook
from promptcoach_client import claude_code, gitdiff

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "promptcoach" / "hooks"


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Take the Windows branch here. Popen loses ``creationflags`` (which only
    Windows accepts) after it is recorded; POSIX-only calls are recorded and
    must not happen."""
    calls: list[tuple[str, Any]] = []
    real_popen = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> Any:
        calls.append(("popen", sorted(kwargs)))
        if os.name != "nt":
            calls.append(("creationflags", kwargs.pop("creationflags", None)))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(gitdiff, "WINDOWS", True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", lambda *a: calls.append(("killpg", a)), raising=False)
    monkeypatch.setattr(
        signal, "pthread_sigmask", lambda *a: calls.append(("mask", a)), raising=False
    )
    return calls


def test_start_options_per_platform() -> None:
    windows = gitdiff._start_options(windows=True)
    assert windows == {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    posix = gitdiff._start_options(windows=False)
    assert set(posix) == {"start_new_session", "preexec_fn"}
    assert posix["start_new_session"] is True


def test_windows_git_runs_without_a_group_or_mask(
    repo: Path, windows: list[tuple[str, Any]]
) -> None:
    out = gitdiff.run_git(["rev-parse", "--is-inside-work-tree"], repo)

    assert out is not None and out.text.strip() == "true"
    names = [name for name, _ in windows]
    assert "mask" not in names and "killpg" not in names
    [(_, kwargs)] = [c for c in windows if c[0] == "popen"]
    assert "start_new_session" not in kwargs and "preexec_fn" not in kwargs
    assert "creationflags" in kwargs
    assert not gitdiff._LIVE


def test_windows_git_that_hangs_is_killed_in_time(
    windows: list[tuple[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in git in Python, so this runs on Windows too."""
    hang = (sys.executable, "-c", "import time; time.sleep(30)")
    monkeypatch.setattr(gitdiff, "GIT_COMMAND", hang)

    started = time.monotonic()
    assert gitdiff.run_git(["status"], tmp_path, timeout=0.5) is None

    assert time.monotonic() - started < 3
    assert "killpg" not in [name for name, _ in windows]
    assert not gitdiff._LIVE


def test_the_per_test_timeout_works_without_sigalarm() -> None:
    """The Windows fallback (a timer and a simulated SIGINT), run here too."""
    import conftest

    started = time.monotonic()
    with pytest.raises(conftest.TestTimedOut), conftest.interrupt_after(0.2):
        while time.monotonic() - started < 10:
            time.sleep(0.01)
    assert conftest.TIMED_OUT == [True]
    conftest.TIMED_OUT.clear()


def test_windows_kill_live_terminates_each_process(
    windows: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(gitdiff, "_LIVE", {111, 222})

    gitdiff._kill_live()

    assert sorted(killed) == [(111, signal.SIGTERM), (222, signal.SIGTERM)]
    assert set() == gitdiff._LIVE
    assert "killpg" not in [name for name, _ in windows]


def test_windows_kill_live_survives_a_gone_process(
    windows: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", gone)
    monkeypatch.setattr(gitdiff, "_LIVE", {333})

    gitdiff._kill_live()

    assert set() == gitdiff._LIVE


def test_windows_on_term_never_reads_the_signal_mask(
    windows: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    monkeypatch.setattr(gitdiff, "_PREVIOUS_TERM", lambda signum, frame: seen.append(signum))

    gitdiff._on_term(signal.SIGTERM, None)

    assert seen == [signal.SIGTERM]
    assert "mask" not in [name for name, _ in windows]


def test_read_control_without_posix_open_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no O_NOFOLLOW or O_NONBLOCK: an lstat check refuses
    symlinks and anything not a regular file before the open."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    (tmp_path / "ok").write_text("../..\n")
    (tmp_path / "dir").mkdir()
    (tmp_path / "big").write_text("x" * 5_000)

    assert gitdiff._read_control(tmp_path / "ok") == (True, "../..\n")
    assert gitdiff._read_control(tmp_path / "missing") == (True, "")
    assert gitdiff._read_control(tmp_path / "dir") == (False, "")
    assert gitdiff._read_control(tmp_path / "big") == (False, "")
    try:
        (tmp_path / "link").symlink_to(tmp_path / "ok")
    except OSError:  # Windows without the symlink privilege
        return
    assert gitdiff._read_control(tmp_path / "link") == (False, "")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on Windows to open")
def test_read_control_without_posix_flags_never_opens_a_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "O_NONBLOCK")
    os.mkfifo(tmp_path / "fifo")

    assert gitdiff._read_control(tmp_path / "fifo") == (False, "")  # would block if opened


# --- text: UTF-8 whatever the locale, CRLF ------------------------------------------------


def test_stdio_is_read_and_written_as_utf8() -> None:
    raw_in = io.BytesIO("caf\u00e9 \u2713".encode())
    raw_out = io.BytesIO()
    stdin = io.TextIOWrapper(raw_in, encoding="cp1252")
    stdout = io.TextIOWrapper(raw_out, encoding="cp1252")

    coach_hook.utf8(stdin)
    coach_hook.utf8(stdout)
    text = stdin.read()
    stdout.write(text)
    stdout.flush()

    assert text == "caf\u00e9 \u2713"
    assert raw_out.getvalue() == "caf\u00e9 \u2713".encode()


def test_utf8_leaves_a_stream_without_reconfigure_alone() -> None:
    stream = io.StringIO("x")
    coach_hook.utf8(stream)
    assert stream.read() == "x"


def test_hook_reads_a_utf8_payload_under_a_cp1252_locale(
    server: StubServer, env: dict[str, str], project: Path
) -> None:
    """As Windows runs it: Python's stdio defaults to the locale's code page."""
    prompt = "caf\u00e9 \u2713 \u05e9\u05dc\u05d5\u05dd"
    done = subprocess.run(
        [sys.executable, str(HOOKS / "coach_hook.py"), "before"],
        input=json.dumps(prompt_payload(project, prompt), ensure_ascii=False).encode(),
        env={**os.environ, **env, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0
    [req] = server.requests
    assert req.body["prompt"] == prompt


def test_crlf_payload_and_command(server: StubServer, env: dict[str, str], project: Path) -> None:
    payload = json.dumps(prompt_payload(project, "coach, quieter\r\n"), indent=1)
    server.reply = {"message": "Coach: quieter now.", "block_prompt": True, "prompt_only": False}

    out = run_hook("before", payload.replace("\n", "\r\n"), env)

    assert json.loads(out) == {"decision": "block", "reason": "Coach: quieter now."}


def test_crlf_git_output_is_read_line_by_line(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Path, Path, Path]] = []
    out = gitdiff.GitOutput("C:/r\r\nC:/r/.git\r\nC:/r/.git\r\n", False)
    monkeypatch.setattr(gitdiff, "run_git", lambda *a, **k: out)

    def own(root: Path, git_dir: Path, common: Path, deadline: object) -> bool:
        seen.append((root, git_dir, common))
        return True

    monkeypatch.setattr(gitdiff, "_own_git_dir", own)

    assert gitdiff.repo_root(Path("C:/r")) == Path("C:/r")
    assert seen == [(Path("C:/r"), Path("C:/r/.git"), Path("C:/r/.git"))]


# --- Windows paths ------------------------------------------------------------------------


def test_backslash_cwd_and_transcript_path_still_coach(
    server: StubServer, env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows ``cwd`` that isn't a directory here: no diff, still coached."""
    monkeypatch.chdir(tmp_path)
    server.reply = {"message": "Say which file.", "block_prompt": False, "prompt_only": False}
    payload = prompt_payload(tmp_path, transcript_path="C:\\Users\\Ana Li\\.claude\\t.jsonl") | {
        "cwd": "C:\\Users\\Ana Li\\proj"
    }
    env["CLAUDE_PROJECT_DIR"] = "C:\\Users\\Ana Li\\proj"

    out = run_hook("before", payload, env)

    assert json.loads(out) == {"systemMessage": "Say which file."}
    assert len(server.requests) == 1


def test_plugin_data_with_a_drive_letter(
    server: StubServer,
    env: dict[str, str],
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Used as a path (joined, never split by hand); absolute on Windows."""
    monkeypatch.chdir(tmp_path)  # on POSIX the name is relative: keep it in tmp_path
    data = "C:\\Users\\Ana Li\\AppData\\promptcoach"
    env["CLAUDE_PLUGIN_DATA"] = data
    config = claude_code.config_from_env(env)
    assert config.data_dir == Path(data)
    if os.name == "nt":
        assert config.data_dir.is_absolute()

    run_hook("before", prompt_payload(project), env)

    state = json.loads((Path(data) / "state.json").read_text())
    assert state["prompt_only"] is False
    shutil.rmtree(Path(data), ignore_errors=True)


def test_unwritable_data_dir_still_blocks(
    server: StubServer, env: dict[str, str], project: Path, tmp_path: Path
) -> None:
    """A data dir that can't be made (a file is in the way): the reply is kept.
    Portable, unlike a chmod 0o555 directory, which Windows ignores."""
    blocker = tmp_path / "in-the-way"
    blocker.write_text("")
    env["CLAUDE_PLUGIN_DATA"] = str(blocker / "data")
    server.reply = {"message": "Coach: quieter now.", "block_prompt": True, "prompt_only": False}

    out = run_hook("before", prompt_payload(project, "coach, quieter"), env)

    assert json.loads(out) == {"decision": "block", "reason": "Coach: quieter now."}
