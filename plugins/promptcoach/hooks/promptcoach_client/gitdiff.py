"""This turn's diff, without touching the person's index, stash or files.

Why snapshots: ``git diff HEAD`` at the end of a turn would blame the agent
for every earlier turn's changes and for the person's own edits. So before
the agent runs, the client records the working tree as a tree object, using
a private index file (``GIT_INDEX_FILE``) seeded from a copy of the real one
so only changed files are hashed. At the end of the turn it takes a second
snapshot the same way and diffs the two trees. Credential files are left out
of both snapshots and of the diff with ``:(exclude)`` pathspecs.

Every git call has a timeout (within the hook's one deadline) and a size
cap, runs with ``GIT_OPTIONAL_LOCKS=0`` so a ``git diff`` never rewrites the
real index's stat cache, never fetches (a partial clone's missing objects
stay missing), and ignores any ``GIT_*`` variables the agent's environment
carries. A repo whose ``.git`` points somewhere else (a ``gitdir:`` file or
``core.worktree``) gets no diff, except a linked worktree its main repo
lists. Submodules show as one commit line, never their content.

The snapshot writes loose objects (blobs and trees) into the repo's own
``.git/objects``; nothing references them, so ``git gc`` removes them.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import MAX_DIFF_CHARS
from .forbidden import exclude_pathspecs, is_forbidden_path

GIT_TIMEOUT_S = 3.0
TRUNCATED = "\n[promptcoach: diff truncated]\n"
_DIFF_BYTES = MAX_DIFF_CHARS * 4  # UTF-8 never needs more for the cap
_SMALL_BYTES = 1_000_000  # rev-parse, write-tree, ls-files
_MIN_STEP_S = 0.05  # less time than this left: a git call can't finish
_NO_DEADLINE_S = 60.0  # for callers without a hook budget (tests, tools)
# Overrides for repo-local settings that would make git run a program or go
# online: an fsmonitor hook, hooks, automatic maintenance, any transport (a
# partial clone fetches missing objects through core.sshCommand), a
# submodule diff format that shows content. Filters are handled by
# ``_repo_defines_filters`` and diff drivers by --no-ext-diff/--no-textconv.
_SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.allow=never",
    "-c",
    "diff.submodule=short",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)
_TRUSTED_SCOPES = frozenset({"global", "system"})
_DIFF_FLAGS = (
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--submodule=short",
    "--ignore-submodules=dirty",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


class Deadline:
    """The end of a hook's time budget, on the monotonic clock.

    One per hook run: every git call and the request get what is left of
    it (capped at their own limit), and a step that would start after it
    is skipped, so the hook as a whole ends on time.
    """

    def __init__(self, seconds: float, start: float | None = None) -> None:
        """``seconds`` from ``start`` (a ``time.monotonic()`` reading; now if None)."""
        self.end = (time.monotonic() if start is None else start) + seconds

    def left(self, cap: float = math.inf) -> float:
        """Seconds left, at most ``cap``; never negative."""
        return max(min(cap, self.end - time.monotonic()), 0.0)

    def spent(self) -> bool:
        """True once (almost) nothing is left: don't start anything new."""
        return self.left() < _MIN_STEP_S


@dataclass(frozen=True)
class Area:
    """Where a diff may look: the project's own directory, never a home repo.

    A project can sit inside a bigger repo (a monorepo, or a dotfiles repo
    holding the person's whole home): only the project's subtree is
    snapshotted and diffed, and a repo whose top is the home directory, or
    above it, gets no diff at all.
    """

    project_dir: Path
    home: Path


# Process groups of the git calls running now, killed if the hook is stopped
# (SIGTERM from the agent at its timeout) or exits with one still going.
_LIVE: set[int] = set()
_PREVIOUS_TERM: Any = signal.SIG_DFL
_CLEANUP_INSTALLED: list[bool] = []
# Threads whose SIGTERM came while git was starting there (the signal blocked):
# run_git hands it back to the handler as soon as git is in _LIVE.
_PENDING_TERM: set[int] = set()


# Held while git starts and is recorded in _LIVE, so the SIGTERM handler
# (which runs in the main thread) and the exit cleanup can't run in between
# and miss it. Reentrant: a SIGTERM handled during the exit cleanup takes it
# again in the same thread, and mustn't deadlock.
_STARTING = threading.RLock()


def _kill_live() -> None:
    """Kill every git process group still running."""
    for pgid in list(_LIVE):
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
        _LIVE.discard(pgid)


def _exit_cleanup() -> None:
    """At exit: kill the running git, waiting for one that is just starting."""
    with _STARTING:
        _kill_live()


def _on_term(signum: int, frame: object) -> None:
    """SIGTERM: kill the running git first, then do what SIGTERM did before.

    Python runs this handler late, at its next check: a SIGTERM that came just
    before run_git blocked it can land inside git's start, in this thread.
    Then it waits, and run_git runs it again once git is in _LIVE.
    """
    if signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []):
        _PENDING_TERM.add(threading.get_ident())
        return
    with _STARTING:
        _kill_live()
    if callable(_PREVIOUS_TERM):
        _PREVIOUS_TERM(signum, frame)
        return
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def install_cleanup() -> None:
    """Make sure no git outlives the hook (call once, from the main thread)."""
    global _PREVIOUS_TERM
    if _CLEANUP_INSTALLED:
        return
    _CLEANUP_INSTALLED.append(True)
    atexit.register(_exit_cleanup)
    _PREVIOUS_TERM = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _on_term)


@dataclass(frozen=True)
class GitOutput:
    """What a git command printed, and whether the size cap cut it short."""

    text: str
    truncated: bool


def _env(index_file: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        LC_ALL="C",
    )
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    return env


def run_git(
    args: list[str],
    cwd: Path,
    *,
    timeout: float = GIT_TIMEOUT_S,
    limit: int = _SMALL_BYTES,
    index_file: Path | None = None,
) -> GitOutput | None:
    """Run ``git`` in ``cwd``; None on any failure, non-zero exit or timeout.

    Output beyond ``limit`` bytes isn't read: git is stopped and the result is
    marked truncated, so a huge diff can't fill memory. Git runs in its own
    process group, and a stop kills the whole group: a program git started
    can't outlive it or hold its output open.
    """
    if timeout < _MIN_STEP_S:
        return None
    # SIGTERM is blocked in this thread, and the handler kept out by the
    # lock, from git's start until it is in _LIVE: a stop then always finds
    # it. git itself gets SIGTERM unblocked, so an orphan (a hook killed with
    # SIGKILL) still obeys a later SIGTERM.
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        with _STARTING:
            # git from PATH, as the agent runs it; the arguments are ours alone.
            proc = subprocess.Popen(  # noqa: S603
                ["git", "--no-pager", *_SAFE_CONFIG, *args],  # noqa: S607
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_env(index_file),
                start_new_session=True,
                preexec_fn=functools.partial(
                    signal.pthread_sigmask, signal.SIG_UNBLOCK, (signal.SIGTERM,)
                ),
            )
            _LIVE.add(proc.pid)
    except OSError:  # git missing, cwd gone
        return None
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if threading.get_ident() in _PENDING_TERM:
            _PENDING_TERM.discard(threading.get_ident())
            _on_term(signal.SIGTERM, None)
    chunks: list[bytes] = []
    stdout = proc.stdout
    assert stdout is not None  # noqa: S101 - PIPE always sets it
    reader = threading.Thread(target=lambda: chunks.append(stdout.read(limit + 1)), daemon=True)
    deadline = time.monotonic() + timeout
    reader.start()
    reader.join(timeout)
    truncated = bool(chunks) and len(chunks[0]) > limit
    try:
        if reader.is_alive() or truncated:
            _kill(proc)
            reader.join(1.0)  # the pipe closes with the group, so this returns at once
        code = proc.wait(max(deadline - time.monotonic(), 0.1))
    except subprocess.TimeoutExpired:  # git closed its output but kept running
        _kill(proc)
        proc.wait()
        return None
    finally:
        stdout.close()
        _LIVE.discard(proc.pid)
    if not chunks or (code != 0 and not truncated):
        return None
    return GitOutput(chunks[0][:limit].decode("utf-8", errors="replace"), truncated)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill git and everything it started (its process group)."""
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve(strict=True) == b.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


# The git dir is walked whole before any diff: bounded, failing closed.
_MAX_GIT_DIR_ENTRIES = 100_000
_GIT_DIR_WALK_S = 0.3


def _no_symlinks(git_dir: Path, deadline: Deadline) -> bool:
    """True if everything in ``git_dir`` is a plain file or directory: no
    symlink, FIFO, socket or device (an lstat walk that never follows a
    link), within 100,000 entries and 0.3 s of the hook's budget; False when
    it can't tell (too big, too slow, unreadable).

    Why one walk and not a list of entries: refs, objects (and each fan-out
    directory or pack), the index, info/grafts, shallow... any of them can
    be a symlink into another of the person's repos, and git would serve
    that repo's history as this one's; a FIFO would hang whoever reads it.
    ``hooks/`` is skipped: hooks are often symlinked into the project, and
    git never runs them here (``core.hooksPath`` is forced to /dev/null).
    Hardlinked files are out of scope: objects are content-addressed, so a
    hardlink can only supply an object the attacker already knows.
    """
    stop = min(time.monotonic() + _GIT_DIR_WALK_S, deadline.end)
    seen = 0
    try:
        if git_dir.is_symlink() or not git_dir.is_dir():
            return False
        pending = [str(git_dir)]
        while pending:
            top = pending.pop()
            with os.scandir(top) as entries:
                for entry in entries:
                    if top == str(git_dir) and entry.name == "hooks":
                        continue
                    seen += 1
                    if seen > _MAX_GIT_DIR_ENTRIES or time.monotonic() > stop:
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif not entry.is_file(follow_symlinks=False):
                        return False  # a symlink, FIFO, socket or device
    except OSError:
        return False
    return True


_CONTROL_BYTES = 4096  # gitdir, commondir, alternates: a path or a few


def _read_control(path: Path) -> tuple[bool, str]:
    """A small git control file the plugin reads itself: (ok, text).

    Missing is fine (``(True, "")``). Anything but a regular file of at most
    4 KB of UTF-8 (a FIFO, a symlink, a directory, a huge file) is
    ``(False, "")``: the caller then refuses, so nothing can make the hook
    block on a read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True, ""
    except OSError:
        return False, ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False, ""
        data = os.read(fd, _CONTROL_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _CONTROL_BYTES:
        return False, ""
    try:
        return True, data.decode("utf-8")
    except UnicodeDecodeError:
        return False, ""


def _borrows(git_dir: Path) -> bool:
    """True if a git dir takes refs or objects from elsewhere: a ``commondir``
    file, or a non-empty (or unreadable) ``objects/info/alternates``."""
    if os.path.lexists(git_dir / "commondir"):
        return True
    ok, alternates = _read_control(git_dir / "objects" / "info" / "alternates")
    return not ok or bool(alternates.strip())


def _own_git_dir(root: Path, git_dir: Path, common_dir: Path, deadline: Deadline) -> bool:
    """True if ``git_dir`` is the work tree's own: ``<root>/.git`` holding its
    own refs and objects, or a linked worktree's directory whose main repo
    lists ``<root>`` as that worktree.

    Why: a ``.git`` file (``gitdir: /elsewhere``), ``core.worktree``, or a
    ``.git`` directory with a ``commondir`` file or ``alternates`` in an
    unpacked archive, or symlinks as or inside the git dir (see
    ``_no_symlinks``), could point git at another of the person's repos, and
    its committed files would then go out as this turn's diff.
    """
    if (root / ".git").is_symlink():
        return False
    if _same(git_dir, root / ".git"):
        # The walk comes first: it refuses a FIFO before anything reads it.
        return (
            _no_symlinks(git_dir, deadline) and _same(common_dir, git_dir) and not _borrows(git_dir)
        )
    worktrees = common_dir / "worktrees"
    if not _same(git_dir.parent, worktrees):
        return False
    if not (_no_symlinks(git_dir, deadline) and _no_symlinks(common_dir, deadline)):
        return False
    ok, listed = _read_control(git_dir / "gitdir")
    if not ok or not listed.strip() or _borrows(common_dir):
        return False
    return _same(Path(listed.strip()), root / ".git")


def repo_root(cwd: Path, deadline: Deadline | None = None) -> Path | None:
    """The top of the work tree containing ``cwd``; None outside a repo, or
    when the repo's git directory isn't its own (see ``_own_git_dir``)."""
    deadline = deadline or Deadline(_NO_DEADLINE_S)
    out = run_git(
        [
            "rev-parse",
            "--show-toplevel",
            "--absolute-git-dir",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        cwd,
        timeout=deadline.left(GIT_TIMEOUT_S),
    )
    lines = out.text.splitlines() if out else []
    if len(lines) != 3 or not all(lines):
        return None
    root, git_dir, common_dir = (Path(line) for line in lines)
    return root if _own_git_dir(root, git_dir, common_dir, deadline) else None


def _repo_defines_filters(root: Path, timeout: float = GIT_TIMEOUT_S) -> bool:
    """True if the repo's own config defines a clean/smudge filter (or we can't tell).

    Why: ``git add`` and a work-tree ``git diff`` run a filter's program on
    file contents, and a repo's own ``.git/config`` (unlike the person's
    global config) may come from someone else, e.g. an unpacked archive.
    Such a repo gets no diff at all rather than a program run.
    """
    out = run_git(["config", "--show-scope", "--list"], root, timeout=timeout)
    if out is None:
        return True
    for line in out.text.splitlines():
        scope, _, entry = line.partition("\t")
        if entry.lower().startswith("filter.") and scope not in _TRUSTED_SCOPES:
            return True
    return False


_GLOB_SPECIAL = re.compile(r"([*?\[\]\\])")


def _scope(prefix: str = "") -> list[str]:
    """Pathspecs: the project's subtree (or the whole tree), credential files out.

    The excludes start under the subtree too: with a subtree include and
    top-level ``**/`` excludes, ``git add -A`` (2.50) skips untracked files.
    """
    if not prefix:
        return ["--", ".", *exclude_pathspecs()]
    escaped = _GLOB_SPECIAL.sub(r"\\\1", prefix)
    return ["--", f":(top,literal){prefix}", *exclude_pathspecs(escaped)]


def _target(cwd: Path, deadline: Deadline, area: Area | None) -> tuple[Path, list[str]] | None:
    """The repo's top and the pathspecs for this project; None if no diff may be taken.

    No diff when the repo's top is the home directory or above it (a
    dotfiles repo), or when the project isn't in this repo at all.
    """
    root = repo_root(cwd, deadline)
    if root is None:
        return None
    if area is None:
        return root, _scope()
    try:
        top, home = root.resolve(strict=True), area.home.resolve()
        project = area.project_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if top == home or top in home.parents:
        return None
    if project == top or project in top.parents:
        return root, _scope()
    if top in project.parents:
        return root, _scope(project.relative_to(top).as_posix())
    return None


def snapshot(cwd: Path, data_dir: Path, deadline: Deadline, area: Area | None = None) -> str | None:
    """Record the work tree (minus credential files) as a tree; its sha, or None.

    The real index is only copied, never written. The private index is a
    fresh file in ``data_dir`` each time, deleted afterwards with any lock
    git left on it: a git killed mid-``add`` can't block the next snapshot.
    """
    index_file: Path | None = None
    try:
        target = _target(cwd, deadline, area)
        if target is None:
            return None
        root, scope = target
        if _repo_defines_filters(root, deadline.left(GIT_TIMEOUT_S)):
            return None
        real = run_git(
            ["rev-parse", "--git-path", "index"], root, timeout=deadline.left(GIT_TIMEOUT_S)
        )
        if real is None:
            return None
        real_index = root / real.text.strip()
        data_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=data_dir, prefix="idx-")
        os.close(fd)
        index_file = Path(name)
        if real_index.is_file():
            # copy2 keeps the index's mtime, which git's racy-clean check
            # compares file mtimes with; a newer copy would hide some edits.
            shutil.copy2(real_index, index_file)
        else:
            index_file.unlink()  # git wants no index file rather than an empty one
        added = run_git(
            ["add", "-A", *scope],
            root,
            timeout=deadline.left(GIT_TIMEOUT_S),
            index_file=index_file,
        )
        if added is None:
            return None
        tree = run_git(
            ["write-tree"], root, timeout=deadline.left(GIT_TIMEOUT_S), index_file=index_file
        )
        return tree.text.strip() if tree and tree.text.strip() else None
    except OSError:
        return None
    finally:
        if index_file is not None:
            for leftover in (index_file, Path(f"{index_file}.lock")):
                with contextlib.suppress(OSError):
                    leftover.unlink(missing_ok=True)


def cap_diff(text: str, truncated: bool = False) -> str:
    """Keep a diff within the server's limit, saying so when it was cut."""
    if not truncated and len(text) <= MAX_DIFF_CHARS:
        return text
    return text[: MAX_DIFF_CHARS - len(TRUNCATED)] + TRUNCATED


def _diff(args: list[str], root: Path, deadline: Deadline, scope: list[str]) -> str | None:
    out = run_git(
        [*_DIFF_FLAGS, *args, *scope],
        root,
        timeout=deadline.left(GIT_TIMEOUT_S),
        limit=_DIFF_BYTES,
    )
    return None if out is None else cap_diff(out.text, out.truncated)


def diff_trees(
    cwd: Path, before: str, after: str, deadline: Deadline, area: Area | None = None
) -> str | None:
    """The diff between two snapshots, credential files excluded."""
    target = _target(cwd, deadline, area)
    return None if target is None else _diff([before, after], target[0], deadline, target[1])


def collect_diff(
    cwd: Path, deadline: Deadline | None = None, area: Area | None = None
) -> str | None:
    """The fallback diff when there is no snapshot from the start of the turn.

    ``git diff HEAD``; in a repo with no commits yet, the staged diff with a
    list of untracked files (credential files left out) in front of it.
    None when ``cwd`` isn't in a repo or git is missing. Only the project's
    subtree (see ``Area``).
    """
    deadline = deadline or Deadline(_NO_DEADLINE_S)
    target = _target(cwd, deadline, area)
    if target is None:
        return None
    root, scope = target
    if _repo_defines_filters(root, deadline.left(GIT_TIMEOUT_S)):
        return None
    head = run_git(
        ["rev-parse", "--verify", "-q", "HEAD"], root, timeout=deadline.left(GIT_TIMEOUT_S)
    )
    if head is not None:
        return _diff(["HEAD"], root, deadline, scope)
    staged = _diff(["--cached"], root, deadline, scope)
    listed = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z", *scope],
        root,
        timeout=deadline.left(GIT_TIMEOUT_S),
    )
    untracked = [
        p for p in (listed.text.split("\0") if listed else []) if p and not is_forbidden_path(p)
    ]
    if staged is None:
        return None
    if not untracked:
        return staged
    header = "Untracked files:\n" + "".join(f"  {p}\n" for p in untracked)
    return cap_diff(header + staged)
