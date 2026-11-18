"""How the hooks start Python: one hooks.json for macOS, Linux and Windows.

The hook command is shell form (see docs/windows.md): line 1 is POSIX sh
(``sh -c`` on macOS and Linux, Git Bash on Windows) and ``exec``s Python, so
the shell never reads line 2; PowerShell has no ``exec`` command, skips line
1 and runs line 2. These tests run the command under every shell this
machine has, with a stand-in ``coach_hook.py`` that reports how it was run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = ROOT / "plugins" / "promptcoach" / "hooks" / "hooks.json"
WINDOWS = os.name == "nt"
HOOK = "${CLAUDE_PLUGIN_ROOT}/hooks/coach_hook.py"
# The whole command, pinned: any change to it needs the manual Windows check
# in docs/windows.md again. MODE is the hook's argument.
SH_LINE = (
    "exec sh -c '"
    'case "$OSTYPE" in msys*|cygwin*) c="py python3 python";; *) c="python3";; esac; '
    'for p in $c; do w=$(command -v "$p") || continue; '
    'case "$w" in *WindowsApps*) continue;; esac; '
    'if [ "$p" = py ]; then exec py -3 -I "$0" "$1"; fi; '
    'exec "$p" -I "$0" "$1"; done; exit 0'
    f'\' "{HOOK}" MODE'
)
PS_LINE = (
    "foreach ($n in 'py', 'python3', 'python') { "
    "$c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue"
    " | Where-Object { $_.Source -notlike '*\\WindowsApps\\*' } | Select-Object -First 1; "
    "if ($c) { if ($n -eq 'py') { & $c.Source -3 -I "
    f'"{HOOK}" MODE }} else {{ & $c.Source -I "{HOOK}" MODE }}; exit 0 }} }}; exit 0'
)
COMMAND = SH_LINE + "\n" + PS_LINE
EVENTS = {
    "UserPromptSubmit": ("before", 3, None),
    "PostToolUse": ("record", 2, "Bash"),
    "PostToolUseFailure": ("record", 2, "Bash"),
    "Stop": ("after", 15, None),
}
# The stand-in hook: says how it was started and what it read, as JSON.
REPORTER = (
    "import json, sys\n"
    "data = sys.stdin.buffer.read().decode('utf-8')\n"
    "print(json.dumps({'argv': sys.argv[1:], 'isolated': sys.flags.isolated, 'stdin': data}))\n"
)
PAYLOAD = '{"prompt": "caf\u00e9 \u2713 \u05e9\u05dc\u05d5\u05dd", "cwd": "C:\\\\x"}\r\n'


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


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    """A plugin root (with a space in it, as Windows user names often have)."""
    root = tmp_path / "plugin root"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "coach_hook.py").write_text(REPORTER)
    return root


def _run(
    shell: str,
    mode: str,
    root: Path,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> str:
    """Run the hook command as Claude Code would: ``${CLAUDE_PLUGIN_ROOT}``
    substituted (forward slashes, except native ones for PowerShell)."""
    powershell = Path(shell).stem.lower() in {"powershell", "pwsh"}
    root_text = str(root) if powershell else root.as_posix()
    command = COMMAND.replace("${CLAUDE_PLUGIN_ROOT}", root_text).replace("MODE", mode)
    argv = [shell, "-NoProfile", "-Command", command] if powershell else [shell, "-c", command]
    done = subprocess.run(
        argv,
        input=PAYLOAD.encode("utf-8"),
        capture_output=True,
        env=env if env is not None else dict(os.environ),
        cwd=cwd,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.decode("utf-8")


def test_hooks_json_runs_the_pinned_command_for_every_event() -> None:
    hooks = json.loads(HOOKS_JSON.read_text())["hooks"]

    assert set(hooks) == set(EVENTS)
    for event, (mode, timeout, matcher) in EVENTS.items():
        hook = {"type": "command", "command": COMMAND.replace("MODE", mode), "timeout": timeout}
        group: dict[str, object] = {"matcher": matcher} if matcher else {}
        group["hooks"] = [hook]
        assert hooks[event] == [group]  # shell form: no args, no shell field


def test_this_machine_has_a_shell_to_test_with() -> None:
    """Never a silent pass: every OS in CI has at least one of the shells."""
    assert _posix_shells() or _powershells()


@pytest.mark.parametrize("mode", ["before", "record", "after"])
def test_posix_shells_start_python_isolated_with_stdin_intact(mode: str, plugin_root: Path) -> None:
    shells = _posix_shells()
    if not shells:
        pytest.skip("no Git Bash here; test_powershell_starts_python_with_stdin_intact runs")
    for shell in shells:
        out = json.loads(_run(shell, mode, plugin_root))
        assert out == {"argv": [mode], "isolated": 1, "stdin": PAYLOAD}, shell


@pytest.mark.parametrize("mode", ["before", "record", "after"])
def test_powershell_starts_python_with_stdin_intact(mode: str, plugin_root: Path) -> None:
    shells = _powershells()
    if not shells:
        pytest.skip(
            "no PowerShell here: this runs on the windows-latest CI job; "
            "test_posix_shells_start_python_isolated_with_stdin_intact covers this machine"
        )
    for shell in shells:
        out = json.loads(_run(shell, mode, plugin_root))
        assert out == {"argv": [mode], "isolated": 1, "stdin": PAYLOAD}, shell


def _path_of(tmp_path: Path, **programs: str) -> str:
    """A PATH holding only ``sh`` and the given stand-in programs (sh scripts)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sh = shutil.which("sh")
    assert sh is not None
    (bin_dir / "sh").symlink_to(sh)
    for name, body in programs.items():
        (bin_dir / name).write_text(f"#!{sh}\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return str(bin_dir)


POSIX_ONLY = pytest.mark.skipif(
    WINDOWS,
    reason="POSIX stand-in programs; on Windows test_posix_shells_start_python_isolated_"
    "with_stdin_intact runs the same command under Git Bash",
)


@POSIX_ONLY
def test_no_python_means_a_quiet_exit(tmp_path: Path, plugin_root: Path) -> None:
    shell = shutil.which("sh")
    assert shell is not None
    env = {"PATH": _path_of(tmp_path)}

    assert _run(shell, "before", plugin_root, env) == ""


def _windows_like(tmp_path: Path, programs: dict[str, str]) -> dict[str, str]:
    """dash reads ``OSTYPE`` from the environment (bash would set its own), so
    it can take the Git Bash branch here."""
    stub = tmp_path / "WindowsApps"
    stub.mkdir()
    (stub / "python3").write_text("#!/bin/sh\necho STORE-STUB\n")
    (stub / "python3").chmod(0o755)
    path = _path_of(tmp_path, **programs)
    return {"PATH": f"{stub}{os.pathsep}{path}", "OSTYPE": "msys"}


@POSIX_ONLY
def test_git_bash_prefers_py_dash_3(tmp_path: Path, plugin_root: Path) -> None:
    dash = shutil.which("dash")
    if dash is None:
        pytest.skip("no dash: CI's ubuntu-latest job has it and runs this")
    env = _windows_like(tmp_path, {"py": 'echo "py $*"', "python": 'echo "python $*"'})
    hook = f"{plugin_root.as_posix()}/hooks/coach_hook.py"

    assert _run(dash, "before", plugin_root, env).strip() == f"py -3 -I {hook} before"


@POSIX_ONLY
def test_git_bash_skips_the_store_stub(tmp_path: Path, plugin_root: Path) -> None:
    dash = shutil.which("dash")
    if dash is None:
        pytest.skip("no dash: CI's ubuntu-latest job has it and runs this")
    env = _windows_like(tmp_path, {"python": 'echo "python $*"'})
    hook = f"{plugin_root.as_posix()}/hooks/coach_hook.py"

    assert _run(dash, "record", plugin_root, env).strip() == f"python -I {hook} record"


@POSIX_ONLY
def test_git_bash_with_only_the_store_stub_stays_quiet(tmp_path: Path, plugin_root: Path) -> None:
    dash = shutil.which("dash")
    if dash is None:
        pytest.skip("no dash: CI's ubuntu-latest job has it and runs this")

    assert _run(dash, "after", plugin_root, _windows_like(tmp_path, {})) == ""


@POSIX_ONLY
def test_posix_never_tries_py(tmp_path: Path, plugin_root: Path) -> None:
    """On macOS and Linux a ``py`` on PATH is someone else's program."""
    shell = shutil.which("sh")
    assert shell is not None
    env = {"PATH": _path_of(tmp_path, py='echo "py $*"', python3='echo "python3 $*"')}
    hook = f"{plugin_root.as_posix()}/hooks/coach_hook.py"

    assert _run(shell, "before", plugin_root, env).strip() == f"python3 -I {hook} before"


def test_python_never_runs_a_projects_json_py(tmp_path: Path, plugin_root: Path) -> None:
    """``-I``: a ``json.py`` in the working directory is never imported."""
    shells = _posix_shells() or _powershells()
    project = tmp_path / "evil-project"
    project.mkdir()
    marker = tmp_path / "MARKER"
    (project / "json.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    env = {**os.environ, "PYTHONPATH": str(project)}

    out = _run(shells[0], "before", plugin_root, env, cwd=project)

    assert json.loads(out)["isolated"] == 1
    assert not marker.exists()
