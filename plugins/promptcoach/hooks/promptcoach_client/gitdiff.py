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

import contextlib
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def left(self, cap: float = math.inf) -> float:
        """Seconds left, at most ``cap``; never negative."""
        return max(min(cap, self.end - time.monotonic()), 0.0)

    def spent(self) -> bool:
        """True once (almost) nothing is left: don't start anything new."""
        return self.left() < _MIN_STEP_S


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
    try:
        # git from PATH, as the agent runs it; the arguments are ours alone.
        proc = subprocess.Popen(  # noqa: S603
            ["git", "--no-pager", *_SAFE_CONFIG, *args],  # noqa: S607
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_env(index_file),
            start_new_session=True,
        )
    except OSError:  # git missing, cwd gone
        return None
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


def _borrows(git_dir: Path) -> bool:
    """True if a git dir takes refs or objects from elsewhere: a ``commondir``
    file, or a non-empty ``objects/info/alternates``."""
    if (git_dir / "commondir").exists():
        return True
    try:
        alternates = (git_dir / "objects" / "info" / "alternates").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError):
        return True
    return bool(alternates.strip())


def _own_git_dir(root: Path, git_dir: Path, common_dir: Path) -> bool:
    """True if ``git_dir`` is the work tree's own: ``<root>/.git`` holding its
    own refs and objects, or a linked worktree's directory whose main repo
    lists ``<root>`` as that worktree.

    Why: a ``.git`` file (``gitdir: /elsewhere``), ``core.worktree``, or a
    ``.git`` directory with a ``commondir`` file or ``alternates`` in an
    unpacked archive could point git at another of the person's repos, and
    its committed files would then go out as this turn's diff.
    """
    if _same(git_dir, root / ".git"):
        return _same(common_dir, git_dir) and not _borrows(git_dir)
    worktrees = common_dir / "worktrees"
    if not _same(git_dir.parent, worktrees) or _borrows(common_dir):
        return False
    try:
        listed = (git_dir / "gitdir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return _same(Path(listed), root / ".git")


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
    return root if _own_git_dir(root, git_dir, common_dir) else None


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


def _scope() -> list[str]:
    return ["--", ".", *exclude_pathspecs()]


def snapshot(cwd: Path, data_dir: Path, deadline: Deadline) -> str | None:
    """Record the work tree (minus credential files) as a tree; its sha, or None.

    The real index is only copied, never written. The private index is a
    fresh file in ``data_dir`` each time, deleted afterwards with any lock
    git left on it: a git killed mid-``add`` can't block the next snapshot.
    """
    index_file: Path | None = None
    try:
        root = repo_root(cwd, deadline)
        if root is None or _repo_defines_filters(root, deadline.left(GIT_TIMEOUT_S)):
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
            ["add", "-A", *_scope()],
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


def _diff(args: list[str], root: Path, deadline: Deadline) -> str | None:
    out = run_git(
        [*_DIFF_FLAGS, *args, *_scope()],
        root,
        timeout=deadline.left(GIT_TIMEOUT_S),
        limit=_DIFF_BYTES,
    )
    return None if out is None else cap_diff(out.text, out.truncated)


def diff_trees(cwd: Path, before: str, after: str, deadline: Deadline) -> str | None:
    """The diff between two snapshots, credential files excluded."""
    root = repo_root(cwd, deadline)
    return None if root is None else _diff([before, after], root, deadline)


def collect_diff(cwd: Path, deadline: Deadline | None = None) -> str | None:
    """The fallback diff when there is no snapshot from the start of the turn.

    ``git diff HEAD``; in a repo with no commits yet, the staged diff with a
    list of untracked files (credential files left out) in front of it.
    None when ``cwd`` isn't in a repo or git is missing.
    """
    deadline = deadline or Deadline(_NO_DEADLINE_S)
    root = repo_root(cwd, deadline)
    if root is None or _repo_defines_filters(root, deadline.left(GIT_TIMEOUT_S)):
        return None
    head = run_git(
        ["rev-parse", "--verify", "-q", "HEAD"], root, timeout=deadline.left(GIT_TIMEOUT_S)
    )
    if head is not None:
        return _diff(["HEAD"], root, deadline)
    staged = _diff(["--cached"], root, deadline)
    listed = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"],
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
