# Windows support

What the plugin does differently on Windows, why, and what still needs a
check by hand on a real Windows machine. The macOS and Linux behavior is
unchanged except for how the hooks start Python (below).

## Starting Python

One `hooks.json` has to start a real Python 3.9+ on macOS, Linux, and
Windows with or without Git Bash. When there is no Python, it has to stay
quiet: no blocked prompt, no Microsoft Store window.

### What the docs say (code.claude.com, checked 2026-11-18)

- Exec-form hooks (`command` + `args`) look `command` up on PATH and spawn
  it directly. On Windows it must be a real `.exe`, not a `.cmd` or `.bat`.
- Shell-form hooks run through `sh -c` on macOS and Linux. On Windows they
  run through Git Bash, or through PowerShell when Git Bash isn't installed.
  A `shell` field accepts `bash` or `powershell`.
- There is no per-OS command, and nothing on `python` vs `python3` vs `py`.
- `${CLAUDE_PLUGIN_ROOT}` is written into the command with forward slashes
  (`C:/Users/...`), except in exec form and in PowerShell, which get
  backslashes.

### The candidates

| Design | Where it fails |
|---|---|
| Exec form, `python3` (v0.1.0) | Windows: `python3.exe` is often the Microsoft Store alias stub, which runs no Python (and with no arguments opens the Store). python.org installs `python.exe` and `py.exe`, not `python3.exe`. |
| Exec form, `py` | macOS and Linux have no `py`, so the hook doesn't start at all. |
| Two exec-form hooks (`python3` and `py`) | Each OS gets a hook error on every event from the missing one, and both run where both exist. |
| Shell form, `shell: "bash"` | Windows without Git Bash has no bash. |
| Shell form, `shell: "powershell"` | macOS and Linux have no PowerShell. |
| A launcher script | Something still has to start it: exec form can't run a script on Windows, and shell form is then the same question as below. |
| **Shell form, no `shell` field, a command that is valid in sh and in PowerShell** | Chosen. Its risks are the assumptions listed below. |

### The chosen design

The command has two lines.

1. `exec sh -c '<a small sh script>' "<root>/hooks/coach_hook.py" <mode>`
   - In `sh` (macOS, Linux) and Git Bash this replaces the shell, so the
     shell never reads line 2. sh parses and runs a `-c` string one command
     at a time; the tests check this under sh, bash and dash.
   - The script picks the interpreter. On macOS and Linux it tries only
     `python3`: a `py` there is someone else's program. Under Git Bash
     (`$OSTYPE` is `msys` or `cygwin`) it tries `py -3`, then `python3`, then
     `python`, and skips anything under `WindowsApps` (the Store stubs).
   - With no Python found it exits 0 and prints nothing.
2. A PowerShell `foreach` over `py`, `python3`, `python`, again skipping
   `WindowsApps`; it runs the first one found (`py` with `-3`) and exits 0.
   - PowerShell reaches this line because it has no `exec` command: line 1
     fails with "command not found" and PowerShell moves on to the next
     statement.

Every interpreter runs with `-I`, as before. The rest of the work (the
Python 3.9 check, the old-Python warning) is still done by `coach_hook.py`.

`tests/test_hook_command.py` pins the command and runs it under every shell
a machine has, with a stand-in `coach_hook.py` that reports its arguments,
`-I`, and the bytes it read on stdin. CI runs it on ubuntu, macOS and
Windows (Git Bash, Windows PowerShell 5.1 and PowerShell 7).

Known cost: Python from the Microsoft Store (or a `python3` that lives only
under `WindowsApps`) is never used, since it can't be told apart from the
stub by its path. Such a person gets no coaching until they install Python
from python.org or with uv. This is a choice in favor of never opening the
Store; revisit it if the manual check shows the stub can be told apart
safely.

### Assumptions only a Windows machine can confirm

- [ ] Claude Code writes `${CLAUDE_PLUGIN_ROOT}` into a shell-form command as
      text before the shell sees it. In PowerShell `${CLAUDE_PLUGIN_ROOT}`
      would otherwise be an (empty) PowerShell variable, not the environment
      variable.
- [ ] Claude Code passes the command's newline through to the shell
      unchanged, under both Git Bash and PowerShell.
- [ ] Which PowerShell Claude Code starts (`powershell.exe` 5.1 or `pwsh`),
      with which flags. Line 2 is written for both. An execution policy
      matters only for script files; this is a command, so it shouldn't.
- [ ] A native program started from PowerShell's `-Command` gets the hook's
      stdin (the CI test checks this for `powershell -NoProfile -Command`
      and `pwsh -NoProfile -Command`, but not as Claude Code starts it).
- [ ] The "command not found" error from `exec` in PowerShell is only written
      to stderr, and Claude Code ignores stderr when the hook exits 0.
- [ ] The paths `py.exe`, python.org's `python.exe`, and uv's `python3.exe`
      don't contain `WindowsApps`, and every Store stub does.
- [ ] A plugin root containing `$` or a backtick would break the double
      quotes in either shell; Windows user names rarely contain them.
- [ ] The hook finishes within Claude Code's 3 s limit with the shell's
      start added (Git Bash and PowerShell start slower than `sh`).

## Git on Windows

`gitdiff.py` keeps its POSIX path unchanged. On Windows (chosen by an
injected platform value, so both paths are tested on every OS):

- git starts in a new process group (`CREATE_NEW_PROCESS_GROUP`); there is
  no `start_new_session` and no `preexec_fn`.
- There is no signal masking: Windows has no `pthread_sigmask`, and Claude
  Code can't send it a SIGTERM to handle anyway.
- A git that runs too long is stopped with `proc.kill()` (TerminateProcess).
  That stops git itself, not programs git started; with the safe config the
  plugin forces (no hooks, no filters, no fsmonitor, no transport) git
  starts none for the commands the plugin runs.
- `core.hooksPath` is `os.devnull` (`nul` on Windows), as before.
- Control files are opened without `O_NOFOLLOW` and `O_NONBLOCK`, which
  Windows lacks; a symlink is refused by an `lstat` check first instead.
  Windows has no FIFOs in the file system.

**A git can outlive a killed hook.** If Claude Code kills the hook outright
at its time limit (assume it does, with no handler running), nothing in the
hook runs to stop git. Git has no timeout setting of its own, so the orphan
runs until its command ends: about as long as `git add -A` on the project.
It writes only the plugin's private index and unreferenced objects. A Job
Object with "kill on close" would end git with the hook; see TODO.md.

## Text and paths

- The hook reads stdin and writes stdout as UTF-8, whatever the locale
  (Python 3.9 to 3.14 use cp1252 on most Windows systems by default).
- CRLF line ends in the payload and in git's output are handled.
- `cwd`, `transcript_path` and `CLAUDE_PLUGIN_DATA` may be Windows paths with
  a drive letter and backslashes; they are used as paths, never split by hand.
