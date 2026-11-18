"""How the hooks start Python: one hooks.json for macOS, Linux and Windows.

The hook command is shell form (see docs/windows.md), three lines:

0. ``Set-Alias exec Out-Null``: in PowerShell this makes line 1's ``exec``
   harmless (an ``exec.exe`` or ``exec.bat`` on PATH never runs); in sh it is
   a command that isn't found, and sh goes on.
1. ``exec sh -c '...' sh MODE``: sh (macOS, Linux) and Git Bash replace
   themselves here, so they never read line 2. The script finds a Python
   3.9+ and always exits 0.
2. PowerShell, which does the same.

The plugin root is read from the ``CLAUDE_PLUGIN_ROOT`` environment
variable, never pasted into the command, so nothing in the path is ever
parsed by a shell. These tests run the command under every shell this
machine has, with a stand-in ``coach_hook.py`` that reports how it was run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = ROOT / "plugins" / "promptcoach" / "hooks" / "hooks.json"
WINDOWS = os.name == "nt"
VERSION_CHECK = (
    "import sys; sys.exit(0 if sys.version_info >= (3, 9) "
    "else 3 if sys.version_info >= (3, 4) else 1)"
)
# The whole command, pinned: any change to it needs the manual Windows check
# in docs/windows.md again. MODE is the hook's argument.
ALIAS_LINE = "Set-Alias exec Out-Null"
SH_SCRIPT = (
    'h="$CLAUDE_PLUGIN_ROOT/hooks/coach_hook.py"; m="$1"; '
    '[ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -f "$h" ] || exit 0; '
    'case "$OSTYPE" in msys*|cygwin*) c="py python3 python";; *) c="python3 python";; esac; '
    f'v="{VERSION_CHECK}"; f=; g=; '
    'for p in $c; do w=$(command -v "$p") || continue; '
    'case "$w" in *[Ww][Ii][Nn][Dd][Oo][Ww][Ss][Aa][Pp][Pp][Ss]*) continue;; esac; '
    'if [ "$w" = /usr/bin/python3 ] && command -v xcode-select >/dev/null '
    "&& ! xcode-select -p >/dev/null 2>&1; then continue; fi; "
    'a=; [ "$p" = py ] && a=-3; '
    '"$w" $a -c "$v" </dev/null >/dev/null 2>&1; r=$?; '
    'if [ "$r" = 0 ]; then "$w" $a -I "$h" "$m"; exit 0; fi; '
    'if [ "$r" = 3 ] && [ -z "$f" ]; then f="$w"; g="$a"; fi; done; '
    'if [ -n "$f" ]; then "$f" $g -I "$h" "$m"; fi; exit 0'
)
SH_LINE = f"exec sh -c '{SH_SCRIPT}' sh MODE"
PS_LINE = (
    "$r = $env:CLAUDE_PLUGIN_ROOT; "
    "if ($r) { $h = Join-Path $r 'hooks/coach_hook.py' } else { $h = '' }; "
    "if ($h -and (Test-Path -LiteralPath $h -PathType Leaf)) { "
    f"$v = '{VERSION_CHECK}'; $f = $null; $g = @(); "
    "foreach ($n in 'py', 'python3', 'python') { "
    "$c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue"
    " | Where-Object { $_.Source -notmatch '[\\\\/]WindowsApps[\\\\/]' }"
    " | Select-Object -First 1; "
    "if (-not $c) { continue }; "
    "$a = @(); if ($n -eq 'py') { $a = @('-3') }; "
    "$null = & $c.Source @a -c $v 2>$null; "
    "if ($LASTEXITCODE -eq 0) { & $c.Source @a -I $h MODE; exit 0 }; "
    "if ($LASTEXITCODE -eq 3 -and -not $f) { $f = $c.Source; $g = $a } }; "
    "if ($f) { & $f @g -I $h MODE } }; exit 0"
)
COMMAND = "\n".join([ALIAS_LINE, SH_LINE, PS_LINE])
EVENTS = {
    "UserPromptSubmit": ("before", 5, None),
    "PostToolUse": ("record", 5, "Bash"),
    "PostToolUseFailure": ("record", 5, "Bash"),
    "Stop": ("after", 20, None),
}
MODES = ["before", "record", "after"]
# The stand-in hook: says how it was started and what it read, as JSON.
REPORTER = (
    "import json, sys\n"
    "data = sys.stdin.buffer.read().decode('utf-8')\n"
    "print(json.dumps({'argv': sys.argv[1:], 'isolated': sys.flags.isolated, 'stdin': data}))\n"
)
PAYLOAD = '{"prompt": "caf\u00e9 \u2713 \u05e9\u05dc\u05d5\u05dd", "cwd": "C:\\\\x"}\r\n'
# Plugin roots a shell would misread if the path were pasted into the command.
HOSTILE_ROOTS = [
    "sub$(touch PWNED)",
    "tick`touch PWNED`",
    "O'Brien",
    "with space",
    "amp&semi;(paren)",
    "pct%PATH%",
    "dollar$HOME",
    "na\u00efve-\u65e5\u672c",
]


def _git_bash() -> str | None:
    """Git for Windows' bash (never WSL's ``bash.exe`` in System32)."""
    for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMW6432", "")):
        path = Path(base) / "Git" / "bin" / "bash.exe"
        if base and path.is_file():
            return str(path)
    return None


def _posix_shells() -> list[str]:
    if WINDOWS:
        bash = _git_bash()
        return [bash] if bash else []
    return [s for s in (shutil.which("sh"), shutil.which("bash"), shutil.which("dash")) if s]


def _powershells() -> list[str]:
    return [s for s in (shutil.which("powershell"), shutil.which("pwsh")) if s]


def _needs_powershell() -> list[str]:
    shells = _powershells()
    if not shells:
        pytest.skip(
            "no PowerShell here: this runs on CI (preinstalled on GitHub's ubuntu, "
            "macOS and Windows runners); the sh tests in this file cover this machine"
        )
    return shells


def _make_root(base: Path, name: str = "plugin root", body: str = REPORTER) -> Path:
    root = base / name
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "coach_hook.py").write_text(body)
    return root


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    """A plugin root (with a space in it, as Windows user names often have)."""
    return _make_root(tmp_path)


def _run(
    shell: str,
    mode: str,
    root: Path | None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """Run the hook command as Claude Code would, with ``CLAUDE_PLUGIN_ROOT``
    in the environment; (exit status, stdout)."""
    powershell = Path(shell).stem.lower() in {"powershell", "pwsh"}
    command = COMMAND.replace("MODE", mode)
    argv = [shell, "-NoProfile", "-Command", command] if powershell else [shell, "-c", command]
    run_env = dict(os.environ) if env is None else dict(env)
    run_env.pop("CLAUDE_PLUGIN_ROOT", None)
    if root is not None:
        run_env["CLAUDE_PLUGIN_ROOT"] = str(root)
    done = subprocess.run(
        argv,
        input=PAYLOAD.encode("utf-8"),
        capture_output=True,
        env=run_env,
        cwd=cwd,
        timeout=60,
        check=False,
    )
    return done.returncode, done.stdout.decode("utf-8")


def _reported(
    shell: str,
    mode: str,
    root: Path,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> dict[str, object]:
    code, out = _run(shell, mode, root, env, cwd)
    assert code == 0, shell
    value: dict[str, object] = json.loads(out)
    return value


# --- the pin ------------------------------------------------------------------------------


def test_hooks_json_runs_the_pinned_command_for_every_event() -> None:
    hooks = json.loads(HOOKS_JSON.read_text())["hooks"]

    assert set(hooks) == set(EVENTS)
    for event, (mode, timeout, matcher) in EVENTS.items():
        hook = {"type": "command", "command": COMMAND.replace("MODE", mode), "timeout": timeout}
        group: dict[str, object] = {"matcher": matcher} if matcher else {}
        group["hooks"] = [hook]
        assert hooks[event] == [group]  # shell form: no args, no shell field


def test_the_root_is_never_pasted_into_the_command() -> None:
    """No ``${CLAUDE_PLUGIN_ROOT}`` placeholder: Claude Code would write the
    path into the text, where a shell parses it."""
    assert "${" not in COMMAND
    assert '"$CLAUDE_PLUGIN_ROOT' in SH_SCRIPT
    assert "$env:CLAUDE_PLUGIN_ROOT" in PS_LINE


def test_timeouts_leave_room_for_slow_starts() -> None:
    """Shell, launcher and Python start come on top of the hook's own budget."""
    from promptcoach_client import core

    assert EVENTS["UserPromptSubmit"][1] >= core.BEFORE_BUDGET_S + 2
    assert EVENTS["Stop"][1] >= core.AFTER_BUDGET_S + 5
    assert EVENTS["PostToolUse"][1] >= 5


def test_this_machine_has_a_shell_to_test_with() -> None:
    """Never a silent pass: every OS in CI has at least one of the shells."""
    assert _posix_shells() or _powershells()


# --- POSIX sh and Git Bash ----------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_posix_shells_start_python_isolated_with_stdin_intact(mode: str, plugin_root: Path) -> None:
    shells = _posix_shells()
    if not shells:
        pytest.skip("no Git Bash here; the PowerShell tests in this file cover this machine")
    for shell in shells:
        assert _reported(shell, mode, plugin_root) == {
            "argv": [mode],
            "isolated": 1,
            "stdin": PAYLOAD,
        }, shell


@pytest.mark.parametrize("name", HOSTILE_ROOTS)
def test_hostile_roots_run_nothing_and_still_find_the_hook(name: str, tmp_path: Path) -> None:
    root = _make_root(tmp_path, name)
    for shell in _posix_shells() or _powershells():
        assert _reported(shell, "before", root, cwd=tmp_path)["argv"] == ["before"], shell
    assert not list(tmp_path.rglob("PWNED"))


@pytest.mark.parametrize("mode", MODES)
def test_missing_hook_or_root_is_a_quiet_exit(mode: str, tmp_path: Path) -> None:
    """A stale plugin root ("can't open file", status 2) must never block."""
    for shell in _posix_shells() or _powershells():
        assert _run(shell, mode, tmp_path / "gone") == (0, ""), shell
        assert _run(shell, mode, None) == (0, ""), shell


def _path_of(tmp_path: Path, **programs: str) -> str:
    """A PATH holding only ``sh`` and the given stand-in programs (sh scripts)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sh = shutil.which("sh")
    assert sh is not None
    if not (bin_dir / "sh").exists():
        (bin_dir / "sh").symlink_to(sh)
    for name, body in programs.items():
        (bin_dir / name).write_text(f"#!{sh}\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return str(bin_dir)


def _python(version_status: int, run: str = 'echo "ran $*"') -> str:
    """A stand-in interpreter: its version check (``-c``) exits with
    ``version_status``; a real run does ``run``."""
    return f'if [ "$1" = -c ] || [ "$2" = -c ]; then exit {version_status}; fi\n{run}'


POSIX_ONLY = pytest.mark.skipif(
    WINDOWS,
    reason="sh stand-in programs; on Windows the test_powershell_* tests in this file "
    "cover choosing an interpreter, the Store stub and failing interpreters",
)


@POSIX_ONLY
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "python3",
    [
        _python(1),  # fails its version check
        _python(2),  # "can't open file", or any other status 2
        'echo "Unknown option: -I" >&2; exit 2',  # Python 2 as python3
        _python(0, "exit 2"),  # passes the check, then fails
        _python(0, "exit 1"),
    ],
    ids=["version-1", "version-2", "python2", "run-2", "run-1"],
)
def test_a_failing_interpreter_never_makes_the_hook_fail(
    python3: str, mode: str, tmp_path: Path, plugin_root: Path
) -> None:
    """Exit 2 would block every prompt and loop Stop: always exit 0."""
    sh = shutil.which("sh")
    assert sh is not None

    assert _run(sh, mode, plugin_root, {"PATH": _path_of(tmp_path, python3=python3)}) == (0, "")


@POSIX_ONLY
def test_no_python_means_a_quiet_exit(tmp_path: Path, plugin_root: Path) -> None:
    sh = shutil.which("sh")
    assert sh is not None

    assert _run(sh, "before", plugin_root, {"PATH": _path_of(tmp_path)}) == (0, "")


@POSIX_ONLY
def test_a_newer_python_wins_over_an_old_python3(tmp_path: Path, plugin_root: Path) -> None:
    sh = shutil.which("sh")
    assert sh is not None
    path = _path_of(tmp_path, python3=_python(3), python=_python(0, 'echo "python $*"'))

    code, out = _run(sh, "before", plugin_root, {"PATH": path})

    assert (code, out.strip()) == (0, f"python -I {plugin_root}/hooks/coach_hook.py before")


@POSIX_ONLY
def test_an_old_python3_alone_still_runs_for_its_warning(tmp_path: Path, plugin_root: Path) -> None:
    """3.4 to 3.8: run anyway, so coach_hook can say a newer Python is needed."""
    sh = shutil.which("sh")
    assert sh is not None
    path = _path_of(tmp_path, python3=_python(3, 'echo "old $*"'), python=_python(1))

    code, out = _run(sh, "after", plugin_root, {"PATH": path})

    assert (code, out.strip()) == (0, f"old -I {plugin_root}/hooks/coach_hook.py after")


@POSIX_ONLY
def test_posix_never_tries_py(tmp_path: Path, plugin_root: Path) -> None:
    """On macOS and Linux a ``py`` on PATH is someone else's program."""
    sh = shutil.which("sh")
    assert sh is not None
    env = {"PATH": _path_of(tmp_path, py='echo "py $*"', python3=_python(0, 'echo "python3 $*"'))}

    code, out = _run(sh, "before", plugin_root, env)

    assert (code, out.strip()) == (0, f"python3 -I {plugin_root}/hooks/coach_hook.py before")


def _windows_like(tmp_path: Path, programs: dict[str, str], stub_dir: str) -> dict[str, str]:
    """dash reads ``OSTYPE`` from the environment (bash sets its own), so it
    can take the Git Bash branch here."""
    stub = tmp_path / stub_dir
    stub.mkdir()
    (stub / "python3").write_text("#!/bin/sh\necho STORE-STUB\n")
    (stub / "python3").chmod(0o755)
    path = _path_of(tmp_path, **programs)
    return {"PATH": f"{stub}{os.pathsep}{path}", "OSTYPE": "msys"}


def _dash() -> str:
    dash = shutil.which("dash")
    if dash is None:
        pytest.skip("no dash: CI's ubuntu-latest job has it and runs this")
    return dash


@POSIX_ONLY
def test_git_bash_prefers_py_dash_3(tmp_path: Path, plugin_root: Path) -> None:
    programs = {"py": _python(0, 'echo "py $*"'), "python": _python(0, 'echo "python $*"')}
    env = _windows_like(tmp_path, programs, "WindowsApps")

    code, out = _run(_dash(), "before", plugin_root, env)

    assert (code, out.strip()) == (0, f"py -3 -I {plugin_root}/hooks/coach_hook.py before")


@POSIX_ONLY
@pytest.mark.parametrize("stub_dir", ["WindowsApps", "windowsapps", "WINDOWSAPPS"])
def test_git_bash_skips_the_store_stub_in_any_case(
    stub_dir: str, tmp_path: Path, plugin_root: Path
) -> None:
    env = _windows_like(tmp_path, {"python": _python(0, 'echo "python $*"')}, stub_dir)

    code, out = _run(_dash(), "record", plugin_root, env)

    assert (code, out.strip()) == (0, f"python -I {plugin_root}/hooks/coach_hook.py record")


@POSIX_ONLY
def test_git_bash_with_only_the_store_stub_stays_quiet(tmp_path: Path, plugin_root: Path) -> None:
    env = _windows_like(tmp_path, {}, "WindowsApps")

    assert _run(_dash(), "after", plugin_root, env) == (0, "")


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/python3").exists(),
    reason="the Command Line Tools stub is macOS's /usr/bin/python3 only",
)
@pytest.mark.parametrize(("xcode_status", "runs"), [(2, False), (0, True)])
def test_macos_stub_python3_is_skipped_without_command_line_tools(
    xcode_status: int, runs: bool, tmp_path: Path, plugin_root: Path
) -> None:
    """Without the tools, /usr/bin/python3 opens an install dialog every time."""
    fake = _path_of(tmp_path, **{"xcode-select": f"exit {xcode_status}"})
    env = {"PATH": f"{fake}{os.pathsep}/usr/bin"}

    code, out = _run("/bin/sh", "before", plugin_root, env)

    assert code == 0
    if runs:
        assert json.loads(out)["argv"] == ["before"]
    else:
        assert out == ""


def test_python_never_runs_a_projects_json_py(tmp_path: Path, plugin_root: Path) -> None:
    """``-I``: a ``json.py`` in the working directory is never imported."""
    project = tmp_path / "evil-project"
    project.mkdir()
    marker = tmp_path / "MARKER"
    (project / "json.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    env = {**os.environ, "PYTHONPATH": str(project)}

    for shell in _posix_shells() or _powershells():
        assert _reported(shell, "before", plugin_root, env=env, cwd=project)["isolated"] == 1
    assert not marker.exists()


# --- PowerShell ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_powershell_starts_python_with_stdin_intact(mode: str, plugin_root: Path) -> None:
    for shell in _needs_powershell():
        assert _reported(shell, mode, plugin_root) == {
            "argv": [mode],
            "isolated": 1,
            "stdin": PAYLOAD,
        }, shell


PS_STAND_INS = pytest.mark.skipif(
    WINDOWS,
    reason="PowerShell runs only real .exe files as stand-ins there; on Windows "
    "test_powershell_starts_python_with_stdin_intact runs the real interpreters",
)


def _ps_path(base: Path, programs: dict[str, str], stub_dir: str | None = None) -> str:
    """A PATH of stand-in programs (sh scripts) that pwsh on POSIX can find."""
    bin_dir = base / "psbin"
    bin_dir.mkdir(parents=True)
    dirs = [bin_dir]
    if stub_dir:
        stub = base / stub_dir
        stub.mkdir()
        (stub / "python3").write_text("#!/bin/sh\necho STORE-STUB\n")
        (stub / "python3").chmod(0o755)
        dirs.insert(0, stub)
    for name, body in programs.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return os.pathsep.join(str(d) for d in dirs)


@PS_STAND_INS
def test_powershell_skips_the_store_stub_and_prefers_py(tmp_path: Path, plugin_root: Path) -> None:
    for shell in _needs_powershell():
        programs = {"py": _python(0, 'echo "py $*"'), "python": _python(0, 'echo "python $*"')}
        path = _ps_path(tmp_path / Path(shell).stem, programs, stub_dir="WindowsApps")

        code, out = _run(shell, "before", plugin_root, {**os.environ, "PATH": path})

        assert (code, out.split(" ")[:3]) == (0, ["py", "-3", "-I"]), out


@PS_STAND_INS
def test_powershell_with_only_the_stub_or_no_python_stays_quiet(
    tmp_path: Path, plugin_root: Path
) -> None:
    for shell in _needs_powershell():
        path = _ps_path(tmp_path / Path(shell).stem, {}, stub_dir="WindowsApps")

        assert _run(shell, "before", plugin_root, {**os.environ, "PATH": path}) == (0, "")


@PS_STAND_INS
def test_powershell_never_runs_an_exec_program(tmp_path: Path, plugin_root: Path) -> None:
    """Line 1 under PowerShell: ``exec`` is aliased away, so no exec program runs."""
    for shell in _needs_powershell():
        base = tmp_path / Path(shell).stem
        marker = base / "EXEC-RAN"
        programs = {"exec": f"touch '{marker}'", "python": _python(0, 'echo "python $*"')}
        path = _ps_path(base, programs)

        code, out = _run(shell, "record", plugin_root, {**os.environ, "PATH": path})

        assert (code, out.split(" ")[0]) == (0, "python")
        assert not marker.exists()


@PS_STAND_INS
@pytest.mark.parametrize("python", [_python(1), _python(0, "exit 2")], ids=["version", "run"])
def test_powershell_failing_interpreter_never_fails_the_hook(
    python: str, tmp_path: Path, plugin_root: Path
) -> None:
    for shell in _needs_powershell():
        path = _ps_path(tmp_path / Path(shell).stem, {"python": python})

        assert _run(shell, "after", plugin_root, {**os.environ, "PATH": path}) == (0, "")
