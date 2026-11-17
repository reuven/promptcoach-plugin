"""Fix round 1: repo layouts and repo-local config that try to make git run
programs, fetch, or diff someone else's files (the reviewer's p5, p6 and p7).

Every case runs the real hooks against a real git: a marker file proves a
program ran, and the server stub shows what left the machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import StubServer, git, prompt_payload, run_hook, stop_payload
from promptcoach_client import gitdiff


def _evil(tmp_path: Path, *, then: str = "cat") -> tuple[Path, Path]:
    """A program that leaves a marker when run, and the marker's path.

    It then passes its input through (a filter's job), or runs ``then``.
    """
    marker = tmp_path / "MARKER"
    script = tmp_path / "evil.sh"
    script.write_text(f'#!/bin/sh\necho "ran: $0 $*" >> {marker}\n{then}\n')
    script.chmod(0o755)
    return script, marker


def _not_prompt_only(data_dir: Path) -> None:
    data_dir.mkdir(exist_ok=True)
    (data_dir / "state.json").write_text('{"prompt_only": false}')


def _turn(repo: Path, env: dict[str, str], change: Path, text: str) -> None:
    """A prompt, an edit, a matching Stop, and an unmatched Stop (the fallback)."""
    run_hook("before", prompt_payload(repo), env)
    change.write_text(text)
    assert run_hook("after", stop_payload(repo), env) == ""
    assert run_hook("after", stop_payload(repo, prompt_id="unmatched"), env) == ""


def _diffs(server: StubServer) -> list[object]:
    return [r.body["diff"] for r in server.requests if r.path == "/v1/after"]


# --- I1: a partial clone never fetches a missing object ------------------------------


def test_partial_clone_never_fetches(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    script, marker = _evil(tmp_path, then="exit 1")  # an ssh that fails at once
    (repo / "f.txt").write_text("secret-ish\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "f")
    blob = git(repo, "rev-parse", "HEAD:f.txt").strip()
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    for key, value in [
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
        ("remote.origin.url", "ssh://example.invalid/x"),
        ("remote.origin.promisor", "true"),
        ("core.sshCommand", str(script)),
    ]:
        git(repo, "config", key, value)
    _not_prompt_only(data_dir)

    _turn(repo, env, repo / "f.txt", "changed\n")

    assert not marker.exists()


def test_git_env_forbids_fetching_and_optional_locks() -> None:
    env = gitdiff._env()
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "protocol.allow=never" in gitdiff._SAFE_CONFIG


# --- I2: submodules show as a commit line at most ------------------------------------


@pytest.mark.parametrize("where", ["global", "local"])
def test_submodule_content_never_leaves(
    where: str,
    server: StubServer,
    env: dict[str, str],
    repo: Path,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = tmp_path / "subsrc"
    sub.mkdir()
    git(sub, "init", "-q")
    (sub / ".env").write_text("TOKEN=fake-SUBMODULE-0003\n")
    (sub / "lib.py").write_text("code = 1\n")
    git(sub, "add", "-f", ".env", "lib.py")
    git(sub, "commit", "-q", "-m", "i")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "-q", "add", str(sub), "sub")
    git(repo, "commit", "-q", "-m", "sub")
    gitconfig_home = tmp_path / "gitconfig-home"
    gitconfig_home.mkdir()
    if where == "global":
        (gitconfig_home / ".gitconfig").write_text("[diff]\n\tsubmodule = diff\n")
    else:
        git(repo, "config", "diff.submodule", "diff")
    monkeypatch.setenv("HOME", str(gitconfig_home))  # git reads the person's global config
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _not_prompt_only(data_dir)
    (repo / "sub" / ".env").write_text("TOKEN=fake-SUBMODULE-0004\n")

    _turn(repo, env, repo / "sub" / "lib.py", "code = 2\n")

    sent = json.dumps(_diffs(server))
    assert "SUBMODULE" not in sent
    assert "code = 2" not in sent


# --- I5: a lock left by a killed git never blocks later snapshots ----------------------


def test_snapshot_uses_a_fresh_index_and_leaves_nothing(repo: Path, tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "idx-sess-1.lock").touch()  # what a killed `git add` left behind before

    assert gitdiff.snapshot(repo, data, gitdiff.Deadline(10)) is not None
    assert gitdiff.snapshot(repo, data, gitdiff.Deadline(10)) is not None
    assert sorted(p.name for p in data.iterdir()) == ["idx-sess-1.lock"]


def test_snapshot_cleans_up_the_lock_of_a_killed_add(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    real = gitdiff.run_git

    def killed_add(args: list[str], cwd: Path, **kwargs: object) -> gitdiff.GitOutput | None:
        if args[0] == "add":
            index = kwargs["index_file"]
            assert isinstance(index, Path)
            Path(f"{index}.lock").touch()  # git was killed holding the lock
            return None
        return real(args, cwd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gitdiff, "run_git", killed_add)
    assert gitdiff.snapshot(repo, data, gitdiff.Deadline(10)) is None
    assert list(data.iterdir()) == []


def test_turn_after_a_stale_lock_still_gets_its_own_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    _not_prompt_only(data_dir)
    (data_dir / "idx-sess-1.lock").touch()
    (repo / "app.py").write_text("print('earlier, the person')\n")

    _turn(repo, env, repo / "app.py", "print('this turn')\n")

    snapshot_diff = _diffs(server)[0]
    assert isinstance(snapshot_diff, str)
    assert "+print('this turn')" in snapshot_diff
    assert "-print('earlier, the person')" in snapshot_diff
    assert "-print('v1')" not in snapshot_diff


# --- M1: a .git file or core.worktree pointing elsewhere gets no diff ----------------


def test_gitdir_file_pointing_elsewhere_gets_no_diff(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path, tmp_path: Path
) -> None:
    private = tmp_path / "private"
    private.mkdir()
    git(private, "init", "-q")
    (private / "core.py").write_text("EMPLOYER_CODE = 'top secret algorithm'\n")
    git(private, "add", "-A")
    git(private, "commit", "-q", "-m", "i")
    (project / ".git").write_text(f"gitdir: {private / '.git'}\n")
    _not_prompt_only(data_dir)

    _turn(project, env, project / "readme.txt", "hi\n")

    assert _diffs(server) == [None, None]


def test_core_worktree_pointing_elsewhere_gets_no_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "victimdir"
    victim.mkdir()
    (victim / "app.py").write_text("victim notes\n")
    git(repo, "config", "core.worktree", str(victim))
    _not_prompt_only(data_dir)

    _turn(repo, env, victim / "app.py", "victim notes, edited\n")

    assert _diffs(server) == [None, None]


def test_linked_worktree_still_gets_its_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    tree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(tree))
    _not_prompt_only(data_dir)

    _turn(tree, env, tree / "app.py", "print('in the worktree')\n")

    diffs = _diffs(server)
    assert all(isinstance(d, str) and "+print('in the worktree')" in d for d in diffs)


def test_worktree_file_naming_an_unregistered_worktree_gets_no_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    """A .git file may name a real worktree's gitdir; that worktree must name it back."""
    tree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(tree))
    impostor = tmp_path / "impostor"
    impostor.mkdir()
    (impostor / ".git").write_text((tree / ".git").read_text())
    _not_prompt_only(data_dir)

    _turn(impostor, env, impostor / "x.txt", "x\n")

    assert _diffs(server) == [None, None]


def test_unreadable_worktree_link_is_not_trusted(tmp_path: Path) -> None:
    common = tmp_path / "main" / ".git"
    (common / "worktrees" / "wt").mkdir(parents=True)  # no "gitdir" file in it
    assert not gitdiff._own_git_dir(tmp_path / "wt", common / "worktrees" / "wt", common)


# --- mutation survivors: diff drivers, hooks ------------------------------------------


def test_repo_diff_driver_never_runs(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    script, marker = _evil(tmp_path)
    git(repo, "config", "diff.e.textconv", str(script))
    git(repo, "config", "diff.e.command", str(script))
    git(repo, "config", "diff.external", str(script))
    (repo / ".gitattributes").write_text("* diff=e\n")
    _not_prompt_only(data_dir)

    _turn(repo, env, repo / "app.py", "print('v2')\n")

    assert not marker.exists()
    assert all(isinstance(d, str) and "+print('v2')" in d for d in _diffs(server))


def test_repo_hooks_path_never_runs(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    script, marker = _evil(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-index-change", "pre-commit", "post-checkout"):
        os.symlink(script, hooks / name)
    git(repo, "config", "core.hooksPath", str(hooks))
    _not_prompt_only(data_dir)

    _turn(repo, env, repo / "app.py", "print('v2')\n")

    assert not marker.exists()


# --- the private index keeps git's racy-clean check working ---------------------------


def test_same_size_edit_with_an_unchanged_mtime_is_seen(repo: Path, tmp_path: Path) -> None:
    """Git trusts an entry's stat data only if the file is older than the index.

    The private index is a copy of the real one; with the copy's own (new)
    mtime, an edit that kept size and mtime (coarse timestamps, or a tool
    that restores them) would look clean and be missed.
    """
    git(repo, "config", "core.trustctime", "false")
    stamp = 1_700_000_000
    os.utime(repo / "app.py", (stamp, stamp))
    git(repo, "update-index", "--refresh")
    os.utime(repo / ".git" / "index", (stamp, stamp))
    data = tmp_path / "data"
    before = gitdiff.snapshot(repo, data, gitdiff.Deadline(10))

    (repo / "app.py").write_text("print('v2')\n")  # same size as v1
    os.utime(repo / "app.py", (stamp, stamp))
    after = gitdiff.snapshot(repo, data, gitdiff.Deadline(10))

    assert before is not None and after is not None
    assert before != after


# --- M1 follow-up: a real .git directory that borrows another repo ---------------------


def _private_repo(tmp_path: Path) -> Path:
    private = tmp_path / "private"
    private.mkdir()
    git(private, "init", "-q")
    (private / "core.py").write_text("EMPLOYER_CODE = 'top secret algorithm'\n")
    git(private, "add", "-A")
    git(private, "commit", "-q", "-m", "i")
    return private


def test_commondir_in_a_real_git_dir_gets_no_diff(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path, tmp_path: Path
) -> None:
    private = _private_repo(tmp_path)
    dot_git = project / ".git"
    dot_git.mkdir()
    (dot_git / "HEAD").write_text((private / ".git" / "HEAD").read_text())
    (dot_git / "commondir").write_text(f"{private / '.git'}\n")
    _not_prompt_only(data_dir)

    _turn(project, env, project / "readme.txt", "hi\n")

    assert "EMPLOYER" not in json.dumps(_diffs(server))
    assert _diffs(server) == [None, None]


def test_alternates_to_another_repo_get_no_diff(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path, tmp_path: Path
) -> None:
    private = _private_repo(tmp_path)
    git(project, "init", "-q")
    head = git(private, "rev-parse", "HEAD").strip()
    branch = git(project, "symbolic-ref", "HEAD").strip()
    (project / ".git" / "objects" / "info" / "alternates").write_text(
        f"{private / '.git' / 'objects'}\n"
    )
    ref = project / ".git" / branch
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(head + "\n")
    _not_prompt_only(data_dir)

    _turn(project, env, project / "readme.txt", "hi\n")

    assert "EMPLOYER" not in json.dumps(_diffs(server))
    assert _diffs(server) == [None, None]


def test_empty_alternates_file_is_harmless(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    (repo / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "objects" / "info" / "alternates").write_text("\n")
    _not_prompt_only(data_dir)

    _turn(repo, env, repo / "app.py", "print('v2')\n")

    assert all(isinstance(d, str) and "+print('v2')" in d for d in _diffs(server))
