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

The command has three lines. The plugin root is never written into them:
both shells read it from the `CLAUDE_PLUGIN_ROOT` environment variable,
which Claude Code exports to every hook (`"$CLAUDE_PLUGIN_ROOT"` in sh,
`$env:CLAUDE_PLUGIN_ROOT` in PowerShell, never the `${CLAUDE_PLUGIN_ROOT}`
placeholder that Claude Code would paste into the text). So a root holding
`$(...)`, a backtick, `'`, `&`, `%` or a space is only ever a value, never
shell code; the tests run roots like these and check nothing runs.

0. `Set-Alias exec Out-Null`. In PowerShell this makes line 1's `exec`
   harmless: without it, PowerShell would look `exec` up on PATH and could
   run an `exec.exe`, `exec.cmd` or `exec.bat` someone left there. In sh it
   is a command that isn't found; sh says so on stderr and goes on.
1. `exec sh -c '<script>' sh <mode>`. In sh (macOS, Linux) and Git Bash this
   replaces the shell, so the shell never reads line 2 (sh runs a `-c`
   string one command at a time; the tests check sh, bash and dash). The
   script:
   - exits 0 at once if the root is unset or `hooks/coach_hook.py` is
     missing (a stale root would otherwise make Python exit 2, and exit 2
     blocks every prompt and makes Stop loop);
   - tries `python3`, then `python` (macOS, Linux; `py` there is someone
     else's program), or under Git Bash (`$OSTYPE` `msys`/`cygwin`) `py -3`,
     `python3`, `python`;
   - skips anything under `WindowsApps`, in any case (the Store stubs), and
     on macOS skips `/usr/bin/python3` when `xcode-select -p` fails: that is
     the Command Line Tools stub, which would open an install dialog on
     every event;
   - asks each one for its version first (`-c`, stdin closed, output
     dropped): 3.9+ runs at once; 3.4 to 3.8 is kept in case nothing newer
     turns up, so `coach_hook.py` can still say once that a newer Python is
     needed; Python 2 and anything that fails is skipped;
   - runs the hook without `exec` and then exits 0, whatever Python's
     status was.
2. PowerShell does the same: the same file check, `py -3`, `python3`,
   `python` outside `WindowsApps` (any case, either slash), the same
   version check, and `exit 0` at the end.

Every interpreter runs with `-I`, as before.

Why no `exec` into Python: it would pass Python's status straight back, and
a 2 blocks. The cost is that a hook killed at its timeout may leave Python
running for the rest of its own budget (2.5 s before a prompt, 13.5 s at
the end of a turn); it stops by itself then, and kills its git on the way
out.

`tests/test_hook_command.py` pins the command and runs it under every shell
a machine has, with a stand-in `coach_hook.py` that reports its arguments,
`-I`, and the bytes it read on stdin, and with stand-in interpreters that
fail, are Python 2, are old, or sit under `WindowsApps`. CI runs it on
ubuntu, macOS (both have `pwsh` preinstalled, so the PowerShell line runs
there too) and Windows (Git Bash, Windows PowerShell 5.1 and PowerShell 7).

Known costs:

- Python from the Microsoft Store (or a `python3` that lives only under
  `WindowsApps`) is never used, since it can't be told apart from the stub
  by its path. Such a person gets no coaching until they install Python from
  python.org or with uv. This favors never opening the Store; revisit it if
  the manual check shows the stub can be told apart safely.
- Each candidate costs one extra Python start for its version check.

**With no Python 3.9+, nothing can keep a coach command from Claude.** The
plugin is written in Python, so when no usable Python starts (none at all,
or only one older than 3.9), the command `coach, quieter` reaches Claude as
an ordinary prompt and isn't applied. Older Pythons get a once-a-session
note saying so.

### Timeouts

The hook's own budgets count from when Python starts: 2.5 s before a prompt
(the server answers within about 1.7 s) and 13.5 s at the end of a turn. The
shell, the `py` launcher, Python's start and, on Windows, Defender's first
scan of a new process come on top of that. So hooks.json gives the before
hook 5 s, the record hook 5 s and the Stop hook 20 s. The manual Windows
check should measure the real start cost.

### Assumptions only a Windows machine can confirm

- [ ] Claude Code exports `CLAUDE_PLUGIN_ROOT` to shell-form hooks on Windows
      (the docs say both forms export it), with native backslashes.
- [ ] Claude Code passes the command's newlines through to the shell
      unchanged, under both Git Bash and PowerShell.
- [ ] Which PowerShell Claude Code starts (`powershell.exe` 5.1 or `pwsh`),
      with which flags. Line 2 is written for both. An execution policy
      matters only for script files; this is a command, so it shouldn't.
- [ ] A native program started from PowerShell's `-Command` gets the hook's
      stdin (the CI test checks this for `powershell -NoProfile -Command`
      and `pwsh -NoProfile -Command`, but not as Claude Code starts it).
- [ ] The errors that lines 0 and 1 print (sh's "Set-Alias: not found",
      PowerShell's parameter error from `Out-Null`) go only to stderr, which
      Claude Code ignores when the hook exits 0.
- [ ] The paths `py.exe`, python.org's `python.exe`, and uv's `python3.exe`
      don't contain `WindowsApps`, and every Store stub does.
- [ ] PowerShell passes Python's output through intact. The hook's output is
      ASCII JSON (`json.dumps` escapes the rest), so re-encoding can't hurt it.
- [ ] The before hook finishes within 5 s with the shell's and Python's start
      added, also on a cold start with Defender scanning.

## Git on Windows

`gitdiff.py` keeps its POSIX path unchanged. On Windows (chosen by an
injected platform value, so both paths are tested on every OS):

- git starts in a new process group (`CREATE_NEW_PROCESS_GROUP`); there is
  no `start_new_session` and no `preexec_fn`.
- There is no signal masking: Windows has no `pthread_sigmask`, and Claude
  Code can't send it a SIGTERM to handle anyway.
- Each git goes into a Job Object set to kill everything in it when the
  job closes (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, through `ctypes`), right
  after it starts. Git for Windows' `cmd\git.exe` is a wrapper that starts
  the real `git.exe`; killing only the wrapper would leave the real git
  holding the output pipe. On a timeout the job is terminated, which stops
  both; when git finishes, closing the job ends anything git left behind.
  If a job can't be made, `taskkill /T /F /PID` is the fallback.
- run_git stops waiting for git's output 0.2 s after a kill, even if
  something still holds the pipe, so a stuck git can't hold the hook past
  its deadline.
- `core.hooksPath` is `os.devnull` (`nul` on Windows), as before.
- Control files are opened without `O_NOFOLLOW` and `O_NONBLOCK`, which
  Windows lacks, and with `O_BINARY`. A symlink, or anything else not a
  regular file, is refused by an `lstat` check before the open, and the
  opened file must be the one checked (`os.path.samestat`). Windows has no
  FIFOs in the file system.

**When the hook itself is killed.** Windows closes a killed process's
handles, and closing the last handle of a kill-on-close job ends every
process in it, so git ends with the hook. Two gaps remain: a process git
starts in the instant between git's start and its assignment to the job is
outside the job; and if the job couldn't be made (the `taskkill` fallback),
an orphaned git runs until its command ends, about as long as `git add -A`
on the project, writing only the plugin's private index and unreferenced
objects.

## Text and paths

- The hook reads stdin and writes stdout as UTF-8, whatever the locale
  (Python 3.9 to 3.14 use cp1252 on most Windows systems by default).
- CRLF line ends in the payload and in git's output are handled.
- `cwd`, `transcript_path` and `CLAUDE_PLUGIN_DATA` may be Windows paths with
  a drive letter and backslashes; they are used as paths, never split by hand.
