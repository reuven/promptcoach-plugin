"""Windows: the git path, text encodings, CRLF and Windows paths.

The Windows branch of ``gitdiff`` is chosen by ``gitdiff.WINDOWS``; these
tests set it, so both branches run (and count for coverage) on every OS.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import coach_hook
from conftest import StubServer, posix_only, prompt_payload, run_hook
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


def test_start_options_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both branches on every OS: the POSIX one gets a fake signal mask
    (Windows has no pthread_sigmask or SIG_UNBLOCK)."""
    masked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(signal, "pthread_sigmask", lambda *a: masked.append(a), raising=False)
    monkeypatch.setattr(signal, "SIG_UNBLOCK", 1, raising=False)
    windows = gitdiff._start_options(windows=True)
    assert windows == {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    posix = gitdiff._start_options(windows=False)
    assert set(posix) == {"start_new_session", "preexec_fn"}
    assert posix["start_new_session"] is True
    posix["preexec_fn"]()
    assert masked == [(1, (signal.SIGTERM,))]


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
    (tmp_path / "ok").write_bytes(b"../..\n")  # write_text writes CRLF on Windows
    (tmp_path / "dir").mkdir()
    (tmp_path / "big").write_bytes(b"x" * 5_000)

    assert gitdiff._read_control(tmp_path / "ok") == (True, "../..\n")
    assert gitdiff._read_control(tmp_path / "missing") == (True, "")
    assert gitdiff._read_control(tmp_path / "dir") == (False, "")
    assert gitdiff._read_control(tmp_path / "big") == (False, "")
    try:
        (tmp_path / "link").symlink_to(tmp_path / "ok")
    except OSError:  # Windows without the symlink privilege
        pytest.skip("no symlink privilege here; the checks above ran, the symlink case didn't")
    assert gitdiff._read_control(tmp_path / "link") == (False, "")


def test_read_control_refuses_a_file_swapped_after_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without O_NOFOLLOW the lstat and the open are two steps: the opened
    file must be the one that was checked."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    (tmp_path / "ok").write_bytes(b"../..\n")
    (tmp_path / "other").write_bytes(b"elsewhere\n")
    real_lstat = os.lstat
    monkeypatch.setattr(os, "lstat", lambda path: real_lstat(tmp_path / "other"))

    assert gitdiff._read_control(tmp_path / "ok") == (False, "")


def test_read_control_opens_in_binary_mode_where_there_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows' O_BINARY: no CRLF translation of the control file."""
    flags: list[int] = []
    real_open = os.open

    def opened(path: Any, flag: int, *args: Any) -> int:
        flags.append(flag)
        return real_open(path, flag & ~0x8000, *args)

    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setattr(os, "open", opened)
    (tmp_path / "ok").write_bytes(b"x\n")

    assert gitdiff._read_control(tmp_path / "ok") == (True, "x\n")
    assert flags and all(f & 0x8000 for f in flags)


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


# --- a git that won't die: its children hold the pipe -------------------------------------

# A stand-in git that starts a grandchild holding stdout, writes its pid, and waits.
GRANDCHILD_GIT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(30)\n"
)


def _grandchild_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "git.py"
    script.write_text(GRANDCHILD_GIT)
    pid_file = tmp_path / "grandchild.pid"
    monkeypatch.setattr(gitdiff, "GIT_COMMAND", (sys.executable, str(script), str(pid_file)))
    return pid_file


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _reap(pid_file: Path) -> None:
    if pid_file.exists():
        with contextlib.suppress(OSError):
            os.kill(int(pid_file.read_text()), signal.SIGTERM)


class FakeJobs:
    """The Job Object layer, faked: records calls; terminate kills what was
    assigned (and, as a real job would, the grandchild)."""

    def __init__(
        self, pid_file: Path | None = None, create_ok: bool = True, assign_ok: bool = True
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.pid_file = pid_file
        self.create_ok = create_ok
        self.assign_ok = assign_ok

    def create(self) -> int | None:
        self.calls.append(("create", None))
        return 42 if self.create_ok else None

    def assign(self, job: int, proc: subprocess.Popen[bytes]) -> bool:
        self.calls.append(("assign", proc.pid))
        return self.assign_ok

    def terminate(self, job: int) -> None:
        self.calls.append(("terminate", job))
        for name, pid in self.calls:
            if name == "assign":
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)

    def close(self, job: int) -> None:
        self.calls.append(("close", job))


@pytest.mark.skipif(os.name == "nt", reason="on Windows the real Job Object test runs instead")
def test_windows_timeout_terminates_the_job_and_returns_by_the_deadline(
    windows: list[tuple[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grandchild keeps the pipe open (the fake can't kill it, as a
    wrapper git.exe's real git): run_git still returns on time."""
    pid_file = _grandchild_git(tmp_path, monkeypatch)
    jobs = FakeJobs()
    monkeypatch.setattr(gitdiff, "_JOBS", jobs)
    try:
        started = time.monotonic()
        assert gitdiff.run_git(["status"], tmp_path, timeout=1.0) is None
        assert time.monotonic() - started < 2.5
    finally:
        _reap(pid_file)
    names = [name for name, _ in jobs.calls]
    assert names == ["create", "assign", "terminate", "close"]
    assert not gitdiff._LIVE and not gitdiff._JOB_OF


def test_windows_git_that_finishes_closes_its_job(
    repo: Path, windows: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = FakeJobs()
    monkeypatch.setattr(gitdiff, "_JOBS", jobs)

    assert gitdiff.run_git(["rev-parse", "--is-inside-work-tree"], repo) is not None
    assert [name for name, _ in jobs.calls] == ["create", "assign", "close"]


@pytest.mark.skipif(os.name == "nt", reason="on Windows the real Job Object test runs instead")
def test_windows_without_a_job_falls_back_to_taskkill(
    windows: list[tuple[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _grandchild_git(tmp_path, monkeypatch)
    monkeypatch.setattr(gitdiff, "_JOBS", FakeJobs(create_ok=False))
    ran: list[list[str]] = []
    monkeypatch.setattr(gitdiff, "_run_taskkill", lambda argv, timeout: ran.append(argv))
    try:
        assert gitdiff.run_git(["status"], tmp_path, timeout=0.5) is None
    finally:
        _reap(pid_file)
    assert len(ran) == 1
    assert ran[0][:4] == ["taskkill", "/T", "/F", "/PID"]


def test_windows_kill_live_terminates_jobs_first(
    windows: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = FakeJobs()
    killed: list[int] = []
    monkeypatch.setattr(gitdiff, "_JOBS", jobs)
    monkeypatch.setattr(gitdiff, "_JOB_OF", {111: 7})
    monkeypatch.setattr(gitdiff, "_LIVE", {111, 222})
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(gitdiff, "_run_taskkill", lambda argv, timeout: None)

    gitdiff._kill_live()

    assert ("terminate", 7) in jobs.calls
    assert 222 in killed


def test_run_taskkill_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(subprocess, "run", missing)
    gitdiff._run_taskkill(["taskkill", "/T", "/F", "/PID", "1"], 1.0)


@posix_only("test_windows_timeout_terminates_the_job_and_returns_by_the_deadline", "killpg")
def test_posix_timeout_kills_the_grandchild_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _grandchild_git(tmp_path, monkeypatch)
    try:
        started = time.monotonic()
        assert gitdiff.run_git(["status"], tmp_path, timeout=1.0) is None
        assert time.monotonic() - started < 2.5
        deadline = time.monotonic() + 3
        while _alive(int(pid_file.read_text())) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(int(pid_file.read_text()))
    finally:
        _reap(pid_file)


@pytest.mark.skipif(
    os.name != "nt",
    reason="the real Job Object needs Windows; elsewhere "
    "test_windows_timeout_terminates_the_job_and_returns_by_the_deadline runs it faked",
)
def test_real_job_object_kills_a_process_tree(tmp_path: Path) -> None:  # pragma: no cover
    jobs = gitdiff._WinJobs()
    job = jobs.create()
    assert job is not None
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert jobs.assign(job, proc)
        jobs.terminate(job)
        assert proc.wait(5) is not None
    finally:
        jobs.close(job)
        proc.kill()


def test_exit_cleanup_waits_for_a_git_that_is_starting_and_kills_it(
    windows: list[tuple[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit cleanup waits (up to 0.3 s) for a git still starting in another
    thread, then kills it: the in-process twin of test_round4's
    test_exit_while_another_thread_starts_git_kills_it, with a Python
    stand-in git, so it runs on Windows too."""
    pid_file = _grandchild_git(tmp_path, monkeypatch)
    if os.name != "nt":
        monkeypatch.setattr(gitdiff, "_JOBS", FakeJobs())
    current = subprocess.Popen
    started = threading.Event()
    procs: list[Any] = []

    def slow_popen(*args: Any, **kwargs: Any) -> Any:
        proc = current(*args, **kwargs)
        procs.append(proc)
        started.set()
        time.sleep(0.2)  # git runs, not yet in _LIVE
        return proc

    monkeypatch.setattr(subprocess, "Popen", slow_popen)
    thread = threading.Thread(
        target=gitdiff.run_git, args=(["status"], tmp_path), kwargs={"timeout": 30}, daemon=True
    )
    try:
        thread.start()
        assert started.wait(10)
        gitdiff._exit_cleanup()
        assert procs[0].wait(5) is not None
    finally:
        _reap(pid_file)
        for proc in procs:
            proc.kill()
        gitdiff._RESUME.set()  # the thread parked (test_exit_park.py)
        thread.join(5)


def test_windows_job_that_cannot_take_git_is_closed_at_once(
    repo: Path, windows: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AssignProcessToJobObject can fail (git already in a job that forbids
    nesting): the job is closed, and git runs untracked by any job."""
    jobs = FakeJobs(assign_ok=False)
    monkeypatch.setattr(gitdiff, "_JOBS", jobs)

    assert gitdiff.run_git(["rev-parse", "--is-inside-work-tree"], repo) is not None
    assert [name for name, _ in jobs.calls] == ["create", "assign", "close"]
    assert not gitdiff._JOB_OF


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("posix_flags", [True, False], ids=["posix-open", "windows-open"])
def test_read_control_returns_the_bytes_as_written(
    ending: str, posix_flags: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No newline translation either way (O_BINARY on Windows): git writes
    LF, a hand edit may leave CRLF, and the callers strip both."""
    if not posix_flags:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    elif not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("no O_NOFOLLOW here; the windows-open case covers this OS")
    (tmp_path / "gitdir").write_bytes(f"C:/r/.git{ending}".encode())

    assert gitdiff._read_control(tmp_path / "gitdir") == (True, f"C:/r/.git{ending}")
