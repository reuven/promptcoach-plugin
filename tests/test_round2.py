"""Fix round 2 (re-review): FIFOs among git control files, secrets in command
text and in commands that print them, a parent repo's files, tails cut inside
a key, the hooks/ directory, and killing git when the hook is stopped.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import StubServer, git, prompt_payload, run_hook, stop_payload
from promptcoach_client import core, gitdiff
from promptcoach_client.contract import MAX_OUTPUT_CHARS
from promptcoach_client.redact import prints_secrets, redact

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "promptcoach" / "hooks" / "coach_hook.py"


def _not_prompt_only(data_dir: Path) -> None:
    data_dir.mkdir(exist_ok=True)
    (data_dir / "state.json").write_text('{"prompt_only": false}')


def _diffs(server: StubServer) -> list[object]:
    return [r.body["diff"] for r in server.requests if r.path == "/v1/after"]


def _after_in_a_child(cwd: Path, env: dict[str, str]) -> float:
    """Run the after hook as Claude Code does; its wall time (killed at 25 s)."""
    payload = stop_payload(cwd, prompt_id="unmatched")
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-I", str(HOOK), "after"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env, "CLAUDE_PROJECT_DIR": str(cwd)},
        timeout=25,
        check=False,
    )
    assert (done.returncode, done.stderr) == (0, "")
    return time.monotonic() - started


# --- R1: a FIFO where git control files belong never hangs the hook ----------------------


def test_fifo_alternates_returns_in_time_with_no_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    (repo / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    os.mkfifo(repo / ".git" / "objects" / "info" / "alternates")
    (repo / "app.py").write_text("print('v2')\n")
    _not_prompt_only(data_dir)

    assert _after_in_a_child(repo, env) < core.AFTER_BUDGET_S + 1.0
    assert _diffs(server) == [None]


def test_fifo_worktree_gitdir_returns_in_time_with_no_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    """The reviewer's p14c: a crafted worktree whose gitdir link is a FIFO."""
    tree = tmp_path / "chain_wt"
    tree.mkdir()
    (tree / "x.txt").write_text("hi\n")
    link = repo / ".git" / "worktrees" / "w"
    link.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {link}\n")
    (link / "commondir").write_text("../..\n")
    (link / "HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    os.mkfifo(link / "gitdir")
    _not_prompt_only(data_dir)

    assert _after_in_a_child(tree, env) < core.AFTER_BUDGET_S + 1.0
    assert _diffs(server) == [None]


def test_read_control(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "big").write_text("x" * 5_000)
    (tmp_path / "ok").write_text("../..\n")
    (tmp_path / "link").symlink_to(tmp_path / "ok")
    (tmp_path / "dir").mkdir()

    assert gitdiff._read_control(tmp_path / "ok") == (True, "../..\n")
    assert gitdiff._read_control(tmp_path / "missing") == (True, "")
    for bad in ("big", "link", "dir"):
        assert gitdiff._read_control(tmp_path / bad) == (False, ""), bad


def test_read_control_never_blocks_on_a_fifo(tmp_path: Path) -> None:
    """Fails in 2 s, not never: opening a FIFO without O_NONBLOCK blocks forever."""
    os.mkfifo(tmp_path / "fifo")
    results: list[tuple[bool, str]] = []
    reader = threading.Thread(
        target=lambda: results.append(gitdiff._read_control(tmp_path / "fifo")), daemon=True
    )
    reader.start()
    reader.join(2)
    stuck = reader.is_alive()
    if stuck:  # unblock the stuck open so the thread can end
        with open(tmp_path / "fifo", "w"):
            pass
    assert not stuck
    assert results == [(False, "")]


def test_any_special_file_in_the_git_dir_means_no_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    os.mkfifo(repo / ".git" / "info-fifo")
    _not_prompt_only(data_dir)

    run_hook("after", stop_payload(repo), env)

    assert _diffs(server) == [None]


# --- R2: command text, more secret-printing commands, more token shapes ------------------


def test_command_text_is_redacted() -> None:
    runs = [
        {
            "prompt_id": "p",
            "command": "curl -H 'Authorization: Bearer sk-ant-api03-abcdefghijklmnopqrstuv' x",
            "exit_code": 0,
            "output_tail": "ok",
        },
        {
            "prompt_id": "p",
            "command": "export OPENAI_API_KEY=sk-proj-abcdefghijklmnop && python app.py",
            "exit_code": 0,
            "output_tail": "ok",
        },
    ]
    sent = core._tool_runs(runs, "p", False)
    text = json.dumps(sent)
    assert "abcdefghijklmnop" not in text
    assert all("[REDACTED]" in run["command"] for run in sent)


@pytest.mark.parametrize(
    "command",
    [
        "cat .env*",
        "cat .[e]nv",
        "cat .e?v",
        "cat *.env",
        "cat .env.[a-z]*",
        "cat config/.env.*",
        "cat .e\\nv",
        "cat .en''v",
        'cat .e"n"v',
        "echo $OPENAI_API_KEY",
        "echo ${ANTHROPIC_API_KEY:0:12}",
        "echo $GH_PAT",
        "echo $DB_PASS && ls",
        "echo ${SESSION_SECRET}",
        "python -c 'import os; print(os.environ)'",
        "uv run python -c 'import dotenv; print(dotenv.dotenv_values())'",
        "docker compose config",
        "gh auth token",
        "kubectl get secret x -o yaml",
        "ENV=1 env",
        "cat *",
        "x" * 20_000 + " && cat .env",
        "cat ~/.config/gh/hosts.yml",
        "cat config/secrets.yml",
        "cat config/secrets.yaml",
        "dotenv list",
        "aws configure export-credentials",
    ],
)
def test_more_commands_that_print_secrets(command: str) -> None:
    assert prints_secrets(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo $HOME",
        "ls *.py",
        "pytest tests/*.py",
        "cat .env.example",
        "env python app.py",
        "echo $PATH ${PWD}",
        "cat src/*.md",
    ],
)
def test_ordinary_commands_stay(command: str) -> None:
    assert not prints_secrets(command)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("DB_PASS=hunter2", "hunter2"),
        ("DATABASE_URL=postgres://u:hunter2@h/db", "hunter2"),
        ("see https://admin:s3cret@example.com/x", "s3cret"),
        ("STRIPE=sk_live_51Habcdefghijklmnop", "51Habcdefghijklmnop"),
        ("key rk_live_51Habcdefghijklmnop", "51Habcdefghijklmnop"),
        ("key pk_live_51Habcdefghijklmnop", "51Habcdefghijklmnop"),
        ("HF=hf_abcdefghijklmnopqrstuvwxyz12", "abcdefghijklmnopqrstuvwxyz12"),
        ("tok npm_abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnopqrstuvwxyz0123"),
        ("ya29.a0AfH6SMBabcdefghijklmnop", "a0AfH6SMBabcdefghijklmnop"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk", "eyJzdWIiOiIxIn0"),
        ("Authorization: Basic dXNlcjpodW50ZXIy", "dXNlcjpodW50ZXIy"),
        ("authorization: token ghx0123456789", "ghx0123456789"),
        ("Authorization: Bearer abc.def", "abc.def"),
        ("db:\n  password:\n    hunter2plain\n  host: x", "hunter2plain"),
    ],
)
def test_more_secret_shapes_are_redacted(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert "[REDACTED]" in out


def test_yaml_value_on_the_next_line_keeps_the_rest() -> None:
    assert redact("password:\n  hunter2plain\nhost: x") == "password:\n  [REDACTED]\nhost: x"


# --- N2, revised ruling: secret names by parts; fail closed with three narrow exceptions ---


@pytest.mark.parametrize(
    "line",
    [
        "max_tokens=1024,",
        "max_tokens: int = 1024",
        "num_tokens: int = len(x)",
        "total_input_tokens: int | None = None",
        "cache_read_tokens: Optional[int]",
        "+    self.api_key = api_key",
        "api_key=api_key,",
        "password=password",
        "secret_santa = draw()",
        "password = get_password()",
        'token = os.environ["TOKEN"]',
        'api_key = os.getenv("X")',
        'api_key = os.getenv("X", None)',
        'key = os.environ["API_KEY"]',
        "secret = client.secrets.get(name)",
        "token = make_token(user.id, user.name),",
        "password = get_password()",
        "if password_hash == compute(pw):",
        "token := next_token()",
        "auth_url = build(x)",
        "secret_key: // comment",
        "bypass = 1",
        "url: https://docs.python.org",
    ],
)
def test_code_with_secret_sounding_names_stays(line: str) -> None:
    assert redact(line) == line


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("API_KEY=abc12345", "abc12345"),
        ('password = "hunter2"', "hunter2"),
        ('token: "x9y8z7w6"', "x9y8z7w6"),
        ("client_secret: s3cr3t-v4lue", "s3cr3t-v4lue"),
        ("DB_PASSWORD=short", "short"),
        ("api_key = 'k'", "'k'"),
        ("PASSWORD=/abc123", "/abc123"),
        ("API_KEY==xyz", "xyz"),
        ("token = hunter2", "hunter2"),
        ("secret: x9", "x9"),
        ("MAX_TOKENS = 4096", "4096"),
        ("API_TOKEN = get_token()", "get_token()"),
        ("PGPASSWORD=hunter2", "hunter2"),
        ("token: str", "str"),
        ("api_key=settings.api_key", "settings.api_key"),
        ("password: /abc", "/abc"),
        ("authToken = abc", "abc"),
        ("privateKey: x1", "x1"),
        ("credentials = load", "load"),
        ("max_tokens = 'abc'", "'abc'"),
        ("hidden_tokens = 12", "12"),
        ("api_key = other_key", "other_key"),
        ('password = get_password() or "hunter2"', "hunter2"),
        ('token = f("x") + "s3cret"', "s3cret"),
        ('password = load("hunter2")', "hunter2"),
        ('api_key = os.getenv("X", "fallback-secret")', "fallback-secret"),
        ("password = get(a)(b)", "get(a)(b)"),
        ("dbpassword = hunter2", "hunter2"),
        ("githubtoken: abc", "abc"),
        ("DBPASSWORD=x", "=x"),
        ("my_credential = abc", "abc"),
        ('tokenizer = "bert-base"', "bert-base"),
        ("tokenizer = AutoTokenizer.from_pretrained('gpt2')", "gpt2"),
    ],
)
def test_literal_secrets_are_redacted(line: str, secret: str) -> None:
    out = redact(line)
    assert secret not in out
    assert out.endswith("[REDACTED]")


# --- N1: a tail cut inside a key ----------------------------------------------------------

KEY_BODY = "b3BlbnNzaC1rZXktdjEAAAAA"
KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    + f"{KEY_BODY}\n" * 200
    + "-----END OPENSSH PRIVATE KEY-----\n"
)


def test_key_straddling_the_tail_cut_is_redacted() -> None:
    out = "log line\n" * 300 + KEY + "done\n"
    tail = core._output_tail("./show_key.sh", out, False)
    assert KEY_BODY not in tail
    assert tail.endswith("done\n")


def test_recorded_tail_is_redacted_before_it_is_cut(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    _not_prompt_only(data_dir)
    payload = {
        "session_id": "sess-1",
        "prompt_id": "prompt-1",
        "cwd": str(project),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "./show_key.sh"},
        "tool_response": {"stdout": "log line\n" * 300 + KEY + "done\n", "stderr": ""},
    }
    run_hook("record", payload, env)

    assert KEY_BODY not in (data_dir / "runs-sess-1").read_text()


def test_tail_cut_inside_a_secret_name_is_redacted() -> None:
    """Cut first, and the tail would start at ``WORD=...``: no secret name left."""
    line = "DB_PASSWORD=hunter2plainvalue\n"
    tail = line[len("DB_PASS") :] + "y\n" * 2_000
    tail = tail[:MAX_OUTPUT_CHARS]
    out = "log\n" * 100 + "DB_PASS" + tail
    assert out[-MAX_OUTPUT_CHARS:].startswith("WORD=hunter2plainvalue")
    assert "hunter2plainvalue" not in core._output_tail("./run.sh", out, False)


def test_orphan_key_end_at_the_start_is_redacted() -> None:
    out = redact(f"{KEY_BODY}\n{KEY_BODY}\n-----END OPENSSH PRIVATE KEY-----\nrest")
    assert KEY_BODY not in out
    assert out.endswith("\nrest")


def test_long_command_ending_in_cat_env_drops_output(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    """The reviewer's p18: the stored command is cut at 2,000 chars."""
    _not_prompt_only(data_dir)
    command = "cat > gen.py <<'EOF'\n" + "x = 1\n" * 2000 + "EOF\npython gen.py && cat .env"
    payload = {
        "session_id": "sess-1",
        "prompt_id": "prompt-1",
        "cwd": str(project),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": "PLAIN_VALUE_FROM_ENV_FILE", "stderr": ""},
    }
    run_hook("record", payload, env)
    run_hook("after", stop_payload(project), env)

    assert "PLAIN_VALUE_FROM_ENV_FILE" not in json.dumps(server.requests[-1].body)


def test_handmade_runs_line_with_a_secret_command_drops_output(
    server: StubServer, env: dict[str, str], project: Path, data_dir: Path
) -> None:
    """Send time checks the command too, not only record time (mutant N14)."""
    _not_prompt_only(data_dir)
    run = {"prompt_id": "prompt-1", "command": "cat .env", "exit_code": 0, "output_tail": "X=1"}
    (data_dir / "runs-sess-1").write_text(json.dumps(run) + "\n")

    run_hook("after", stop_payload(project), env)

    assert server.requests[-1].body["tool_runs"][0]["output_tail"] == ""


# --- R3: only the project's own part of a repo; never a home-directory repo -------------


def _commit_all(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")


def test_dotfiles_repo_at_home_is_never_diffed(
    server: StubServer, env: dict[str, str], home: Path, data_dir: Path
) -> None:
    git(home, "init", "-q")
    (home / ".zshrc").write_text("export GH_PAT_X=hunter2plain\n")
    _commit_all(home)
    (home / ".zshrc").write_text("export GH_PAT_X=hunter3plain\n")
    project = home / "projects" / "student"
    project.mkdir(parents=True)
    (project / "app.py").write_text("print(1)\n")
    env = {**env, "CLAUDE_PROJECT_DIR": str(project)}
    _not_prompt_only(data_dir)

    run_hook("before", prompt_payload(project), env)
    run_hook("after", stop_payload(project), env)
    run_hook("after", stop_payload(project, prompt_id="unmatched"), env)

    assert _diffs(server) == [None, None]


def test_repo_above_home_is_never_diffed(
    server: StubServer, env: dict[str, str], tmp_path: Path, data_dir: Path
) -> None:
    git(tmp_path, "init", "-q")
    project = tmp_path / "home" / "work"
    project.mkdir(parents=True)
    env = {**env, "CLAUDE_PROJECT_DIR": str(project), "HOME": str(tmp_path / "home")}
    _not_prompt_only(data_dir)

    run_hook("after", stop_payload(project), env)

    assert _diffs(server) == [None]


def test_parent_repo_sends_only_the_project_subtree(
    server: StubServer, env: dict[str, str], tmp_path: Path, data_dir: Path
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-q")
    (parent / ".zshrc").write_text("export GH_PAT_X=hunter2plain\n")
    (parent / "notes.txt").write_text("v1\n")
    _commit_all(parent)
    (parent / ".zshrc").write_text("export GH_PAT_X=hunter3plain\n")
    (parent / "notes.txt").write_text("private notes v2\n")
    project = parent / "projects" / "student"
    project.mkdir(parents=True)
    (project / "app.py").write_text("print(1)\n")
    env = {**env, "CLAUDE_PROJECT_DIR": str(project)}
    _not_prompt_only(data_dir)

    run_hook("before", prompt_payload(project), env)
    (project / "app.py").write_text("print(2)\n")
    run_hook("after", stop_payload(project), env)
    run_hook("after", stop_payload(project, prompt_id="unmatched"), env)

    sent = json.dumps(_diffs(server))
    assert "hunter" not in sent and "private notes" not in sent and ".zshrc" not in sent


def test_monorepo_subdir_diffs_its_own_subtree(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    sub = repo / "services" / "api"
    sub.mkdir(parents=True)
    (sub / "main.py").write_text("v = 1\n")
    _commit_all(repo)
    env = {**env, "CLAUDE_PROJECT_DIR": str(sub)}
    _not_prompt_only(data_dir)

    run_hook("before", prompt_payload(sub), env)
    (sub / "main.py").write_text("v = 2\n")
    (sub / "new.py").write_text("n = 1\n")
    (repo / "app.py").write_text("print('outside the project')\n")
    run_hook("after", stop_payload(sub), env)
    run_hook("after", stop_payload(sub, prompt_id="unmatched"), env)

    diffs = _diffs(server)
    assert all(isinstance(d, str) and "+v = 2" in d for d in diffs), diffs
    assert "+n = 1" in str(diffs[0])
    assert "outside the project" not in json.dumps(diffs)


def test_scope_escapes_glob_characters_in_the_project_path() -> None:
    scope = gitdiff._scope("w[e]ird*/d?\\x")
    assert scope[1] == ":(top,literal)w[e]ird*/d?\\x"
    assert scope[2] == ":(exclude,glob,icase)w\\[e\\]ird\\*/d\\?\\\\x/**/.env"
    assert gitdiff._scope()[1:3] == [".", ":(exclude,glob,icase)**/.env"]


def test_secrets_in_a_monorepo_subdir_stay_out(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    sub = repo / "services" / "api"
    (sub / "deep").mkdir(parents=True)
    (sub / "main.py").write_text("v = 1\n")
    _commit_all(repo)
    env = {**env, "CLAUDE_PROJECT_DIR": str(sub)}
    _not_prompt_only(data_dir)

    run_hook("before", prompt_payload(sub), env)
    (sub / ".ENV").write_text("LEAKED_VALUE_1\n")
    (sub / "deep" / "server.pem").write_text("LEAKED_VALUE_2\n")
    (sub / "new.py").write_text("n = 1\n")
    run_hook("after", stop_payload(sub), env)

    sent = json.dumps(_diffs(server))
    assert "+n = 1" in sent
    assert "LEAKED_VALUE" not in sent


def test_project_outside_the_repo_gets_no_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {**env, "CLAUDE_PROJECT_DIR": str(elsewhere)}
    (repo / "app.py").write_text("print('v2')\n")
    _not_prompt_only(data_dir)

    run_hook("after", stop_payload(repo), env)

    assert _diffs(server) == [None]


# --- N3: hooks/ may hold symlinks (git never runs them here) -----------------------------


def test_symlinked_hook_still_gets_a_diff(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path
) -> None:
    (repo / "scripts").mkdir()
    (repo / "scripts" / "pre-commit").write_text("#!/bin/sh\n")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").symlink_to(repo / "scripts" / "pre-commit")
    (repo / "app.py").write_text("print('v2')\n")
    _not_prompt_only(data_dir)

    run_hook("after", stop_payload(repo), env)

    assert "+print('v2')" in str(_diffs(server)[0])


def test_symlink_beside_hooks_still_blocks(
    server: StubServer, env: dict[str, str], repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    (tmp_path / "x").write_text("x\n")
    (repo / ".git" / "hooksx").symlink_to(tmp_path / "x")
    _not_prompt_only(data_dir)

    run_hook("after", stop_payload(repo), env)

    assert _diffs(server) == [None]


# --- N5: the submodule defenses, each pinned on its own -----------------------------------


def test_every_git_call_carries_the_submodule_overrides(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    real = subprocess.Popen

    def recording(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording)
    (repo / "app.py").write_text("print('v2')\n")
    assert gitdiff.collect_diff(repo) is not None

    assert calls
    for argv in calls:
        assert "diff.submodule=short" in argv
        assert argv[argv.index("diff.submodule=short") - 1] == "-c"
    diffs = [argv for argv in calls if "diff" in argv[argv.index("--no-pager") + 1 :]]
    assert diffs
    for argv in diffs:
        assert "--submodule=short" in argv and "--ignore-submodules=dirty" in argv


# --- N6: git dies with the hook ------------------------------------------------------------


def test_the_hook_installs_git_cleanup(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], project: Path
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(gitdiff, "install_cleanup", lambda: calls.append(True))
    run_hook("after", stop_payload(project), env)
    assert calls == [True]


def test_kill_live_groups(tmp_path: Path) -> None:
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    gitdiff._LIVE.add(proc.pid)
    try:
        gitdiff._kill_live()
        assert proc.wait(5) != 0
        assert proc.pid not in gitdiff._LIVE
    finally:
        proc.kill()
        proc.wait()


SIGTERM_CHILD = """
import sys, pathlib
sys.path.insert(0, sys.argv[1])
from promptcoach_client import gitdiff
gitdiff.install_cleanup()
gitdiff.run_git(["status"], pathlib.Path(sys.argv[2]), timeout=30)
"""


def test_sigterm_kills_the_running_git(tmp_path: Path) -> None:
    pid_file = tmp_path / "git.pid"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 30\n")
    (fake / "git").chmod(0o755)
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", SIGTERM_CHILD, str(HOOK.parent), str(tmp_path)],
        env={"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"},
    )
    deadline = time.monotonic() + 10
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    git_pid = int(pid_file.read_text())

    child.send_signal(signal.SIGTERM)
    child.wait(10)
    time.sleep(0.2)

    with pytest.raises(ProcessLookupError):
        os.kill(git_pid, 0)


def test_on_term_hands_over_to_the_previous_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(gitdiff, "_PREVIOUS_TERM", lambda signum, frame: seen.append(signum))
    gitdiff._on_term(signal.SIGTERM, None)
    assert seen == [signal.SIGTERM]


def test_on_term_without_a_handler_dies_by_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(gitdiff, "_PREVIOUS_TERM", signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda *a: calls.append(("signal", *a)))
    monkeypatch.setattr(os, "kill", lambda *a: calls.append(("kill", *a)))
    gitdiff._on_term(signal.SIGTERM, None)
    assert calls == [
        ("signal", signal.SIGTERM, signal.SIG_DFL),
        ("kill", os.getpid(), signal.SIGTERM),
    ]


def test_install_cleanup_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[object] = []
    monkeypatch.setattr(gitdiff, "_CLEANUP_INSTALLED", [])
    monkeypatch.setattr(atexit, "register", installed.append)
    monkeypatch.setattr(signal, "signal", lambda *a: installed.append(a))
    gitdiff.install_cleanup()
    gitdiff.install_cleanup()
    assert installed == [gitdiff._kill_live, (signal.SIGTERM, gitdiff._on_term)]


def test_read_control_rejects_bytes_that_are_not_utf8(tmp_path: Path) -> None:
    (tmp_path / "bad").write_bytes(b"\xff\xfe")
    assert gitdiff._read_control(tmp_path / "bad") == (False, "")


def test_missing_project_dir_gets_no_diff(repo: Path, tmp_path: Path) -> None:
    (repo / "app.py").write_text("print('v2')\n")
    area = gitdiff.Area(tmp_path / "gone", tmp_path / "home")
    assert gitdiff.collect_diff(repo, area=area) is None


@pytest.mark.parametrize(
    "text",
    [
        "password:",
        "password:\nhost: x",
        "secrets:\n  db_password: x9y8z7w6q",
        "token:\n\nnext: 1",
    ],
)
def test_yaml_next_line_left_alone(text: str) -> None:
    assert redact(text).split("\n")[0] == text.split("\n")[0]
    assert redact(text).count("[REDACTED]") == text.count("x9y8z7w6q")
