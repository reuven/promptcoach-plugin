"""This turn's diff, without touching the person's index, stash or files.

Why snapshots: ``git diff HEAD`` at the end of a turn would blame the agent
for every earlier turn's changes and for the person's own edits. So before
the agent runs, the client records the working tree as a tree object, using
a private index file (``GIT_INDEX_FILE``) seeded from a copy of the real one
so only changed files are hashed. At the end of the turn it takes a second
snapshot the same way and diffs the two trees. Credential files are left out
of both snapshots and of the diff with ``:(exclude)`` pathspecs.

Every git call has a timeout and a size cap, runs with ``GIT_OPTIONAL_LOCKS=0``
so a ``git diff`` never rewrites the real index's stat cache, and ignores any
``GIT_*`` variables the agent's environment carries.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
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
_DIFF_FLAGS = (
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


@dataclass(frozen=True)
class GitOutput:
    """What a git command printed, and whether the size cap cut it short."""

    text: str
    truncated: bool


def _env(index_file: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
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
    marked truncated, so a huge diff can't fill memory.
    """
    try:
        # git from PATH, as the agent runs it; the arguments are ours alone.
        proc = subprocess.Popen(  # noqa: S603
            ["git", "--no-pager", *args],  # noqa: S607
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_env(index_file),
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
            proc.kill()
            reader.join(1.0)  # the pipe closes with git, so this returns at once
        code = proc.wait(max(deadline - time.monotonic(), 0.1))
    except subprocess.TimeoutExpired:  # git closed its output but kept running
        proc.kill()
        proc.wait()
        return None
    finally:
        stdout.close()
    if not chunks or (code != 0 and not truncated):
        return None
    return GitOutput(chunks[0][:limit].decode("utf-8", errors="replace"), truncated)


def repo_root(cwd: Path, timeout: float = GIT_TIMEOUT_S) -> Path | None:
    """The top of the work tree containing ``cwd``, or None outside a repo."""
    out = run_git(["rev-parse", "--show-toplevel"], cwd, timeout=timeout)
    return Path(out.text.strip()) if out and out.text.strip() else None


def _scope() -> list[str]:
    return ["--", ".", *exclude_pathspecs()]


def snapshot(cwd: Path, index_file: Path, timeout: float = GIT_TIMEOUT_S) -> str | None:
    """Record the work tree (minus credential files) as a tree; its sha, or None.

    The real index is only copied, never written. ``timeout`` is the budget
    for all the git calls together. The private index is deleted afterwards.
    """
    deadline = time.monotonic() + timeout

    def left() -> float:
        return max(deadline - time.monotonic(), 0.05)

    try:
        root = repo_root(cwd, left())
        if root is None:
            return None
        real = run_git(["rev-parse", "--git-path", "index"], root, timeout=left())
        if real is None:
            return None
        real_index = root / real.text.strip()
        index_file.parent.mkdir(parents=True, exist_ok=True)
        index_file.unlink(missing_ok=True)
        if real_index.is_file():
            shutil.copyfile(real_index, index_file)
        if run_git(["add", "-A", *_scope()], root, timeout=left(), index_file=index_file) is None:
            return None
        tree = run_git(["write-tree"], root, timeout=left(), index_file=index_file)
        return tree.text.strip() if tree and tree.text.strip() else None
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            index_file.unlink(missing_ok=True)


def cap_diff(text: str, truncated: bool = False) -> str:
    """Keep a diff within the server's limit, saying so when it was cut."""
    if not truncated and len(text) <= MAX_DIFF_CHARS:
        return text
    return text[: MAX_DIFF_CHARS - len(TRUNCATED)] + TRUNCATED


def _diff(args: list[str], root: Path) -> str | None:
    out = run_git([*_DIFF_FLAGS, *args, *_scope()], root, limit=_DIFF_BYTES)
    return None if out is None else cap_diff(out.text, out.truncated)


def diff_trees(cwd: Path, before: str, after: str) -> str | None:
    """The diff between two snapshots, credential files excluded."""
    root = repo_root(cwd)
    return None if root is None else _diff([before, after], root)


def collect_diff(cwd: Path) -> str | None:
    """The fallback diff when there is no snapshot from the start of the turn.

    ``git diff HEAD``; in a repo with no commits yet, the staged diff with a
    list of untracked files (credential files left out) in front of it.
    None when ``cwd`` isn't in a repo or git is missing.
    """
    root = repo_root(cwd)
    if root is None:
        return None
    if run_git(["rev-parse", "--verify", "-q", "HEAD"], root) is not None:
        return _diff(["HEAD"], root)
    staged = _diff(["--cached"], root)
    listed = run_git(["ls-files", "--others", "--exclude-standard", "-z"], root)
    untracked = [
        p for p in (listed.text.split("\0") if listed else []) if p and not is_forbidden_path(p)
    ]
    if staged is None:
        return None
    if not untracked:
        return staged
    header = "Untracked files:\n" + "".join(f"  {p}\n" for p in untracked)
    return cap_diff(header + staged)
