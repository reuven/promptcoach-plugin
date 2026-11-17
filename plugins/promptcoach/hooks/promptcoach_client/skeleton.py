"""What prompt-only mode sends of a shell command: its argv skeleton.

Why: an inline script (``python -c '...'``, a heredoc, ``printf ... > f.py``)
is code, and prompt-only mode promises no code leaves the machine. The server
applies its own, more detailed skeleton (Ruling P'), but the client must not
send the code in the first place, so this one is deliberately cruder and
stricter: it stops a segment at the first word it can't vouch for. It keeps
enough to tell ``uv run pytest -q tests/`` or ``pip install`` from anything
else, which is all the coach needs in this mode.
"""

from __future__ import annotations

import re

from .contract import MAX_SKILL_CHARS, MAX_TEST_COMMAND_CHARS

MAX_SKELETON_CHARS = 200

_OPERATORS = ("&&", "||", "<<<", "<<-", "<<", ">>", ">&", "<&", "&>", ";", "|", "&", ">", "<")
_SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
_ENV_PREFIX = re.compile(r"([A-Za-z_]\w*)=")
_BARE = re.compile(r"[\w./-]+")
_FLAG = re.compile(r"-{1,2}[A-Za-z0-9][\w-]*")
_BLOB = re.compile(r"(?=[^a-z]*[a-z])(?=[^A-Z]*[A-Z])(?=\D*\d)[\w/-]{16,}")
_ODD_DASH = re.compile(r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")
_WINDOWS_SHELL = re.compile(r"pwsh|powershell|cmd", re.IGNORECASE)
_MAX_WORD_CHARS = 32
_CODE_LETTERS = frozenset("ceEpr")
_CODE_FLAGS = frozenset({"-", "-c", "-e", "-E", "-p", "-r"})
_CODE_LONG_FLAGS = frozenset(
    {"--eval", "--command", "--print", "--exec", "--execute", "--expression", "--code"}
)
# Programs whose next words are code or data, never options: keep the name only.
_SAY_PROGRAMS = frozenset(
    {
        "echo",
        "printf",
        "eval",
        "sed",
        "gsed",
        "awk",
        "gawk",
        "mawk",
        "nawk",
        "sqlite3",
        "psql",
        "mysql",
        "duckdb",
        "clickhouse",
        "deno",
        "export",
    }
)
# Words that start another program: its name is the next word.
_WRAPPERS = frozenset(
    {"sudo", "doas", "env", "time", "nice", "nohup", "timeout", "exec", "command",
     "builtin", "xargs", "stdbuf", "npx", "uvx", "pipx", "bunx"}
)  # fmt: skip
# Subcommands worth keeping, per program; any other positional word goes.
_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "uv": frozenset({"run", "sync", "add", "remove", "lock", "pip", "tool", "venv", "init",
                     "build", "publish", "python", "tree", "export", "format", "install"}),
    "git": frozenset({"status", "diff", "add", "commit", "push", "pull", "fetch", "log", "show",
                      "checkout", "switch", "branch", "merge", "rebase", "stash", "restore",
                      "reset", "init", "clone", "tag", "rm", "mv", "blame", "worktree"}),
    "pip": frozenset({"install", "uninstall", "list", "freeze", "show", "check", "download"}),
    "make": frozenset({"test", "tests", "check", "lint", "all", "build", "install", "clean",
                       "format", "fmt", "typecheck", "docs", "coverage", "ci"}),
    "npm": frozenset({"test", "run", "install", "ci", "build", "lint", "exec"}),
    "ruff": frozenset({"check", "format"}),
    "docker": frozenset({"build", "run", "ps", "compose", "up", "down", "logs", "exec",
                         "images", "pull"}),
    "cargo": frozenset({"test", "build", "run", "check", "clippy", "fmt"}),
    "go": frozenset({"test", "build", "run", "vet", "fmt", "mod"}),
    "pre-commit": frozenset({"run", "install", "autoupdate"}),
}  # fmt: skip
for _alias, _name in (("pip3", "pip"), ("pnpm", "npm"), ("yarn", "npm"), ("podman", "docker")):
    _SUBCOMMANDS[_alias] = _SUBCOMMANDS[_name]
# (program, subcommand) pairs whose next word is a program: ``uv run pytest``.
_RUNS_PROGRAM = frozenset(
    {("uv", "run"), ("uv", "tool"), ("npm", "exec"), ("pnpm", "exec"), ("yarn", "exec")}
    | {(runner, "run") for runner in ("poetry", "pipenv", "pdm", "hatch", "conda", "pipx")}
)
_FILE_LIKE = re.compile(r"\.{1,2}|.*/.*|.*\.[A-Za-z][A-Za-z0-9]{0,7}")
_SKILL = re.compile(r"[\w.:@/ -]{1,80}")

Token = tuple[str, bool, bool]  # (text, is_operator, was_quoted)


def _tokenize(line: str) -> list[Token] | None:
    """Split a shell line into words and operators, or None if a quote is open.

    A word remembers whether any part of it was quoted or escaped, since a
    quoted word is where code and secrets live.
    """
    tokens: list[Token] = []
    i, n = 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
            continue
        op = next((o for o in _OPERATORS if line.startswith(o, i)), None)
        if op is not None:
            tokens.append((op, True, False))
            i += len(op)
            continue
        word, quoted = [], False
        while i < n and not line[i].isspace() and line[i] not in ";&|<>":
            ch = line[i]
            if ch in "'\"":
                end = line.find(ch, i + 1)
                if end < 0:
                    return None
                word.append(line[i + 1 : end])
                quoted, i = True, end + 1
            elif ch == "\\":
                word.append(line[i + 1 : i + 2])
                quoted, i = True, i + 2
            else:
                word.append(ch)
                i += 1
        tokens.append(("".join(word), False, quoted))
    return tokens


def _plain_word(word: str) -> bool:
    """A positional word safe to send: short, plain, not a blob or a secret."""
    return (
        bool(_BARE.fullmatch(word))
        and len(word) <= _MAX_WORD_CHARS
        and not any(_BLOB.fullmatch(part) for part in re.split(r"[./]", word))
        and not _WINDOWS_SHELL.search(word)
    )


def _flag(word: str) -> tuple[str | None, bool]:
    """(the part of a flag to keep, whether the segment ends after it)."""
    if word in _CODE_FLAGS or word in _CODE_LONG_FLAGS:
        return word, True
    if word.startswith("--"):
        name = word.split("=", 1)[0]
        return (name, False) if _FLAG.fullmatch(name) else (None, True)
    head = word[:2]
    if not _FLAG.fullmatch(head):
        return None, True
    # A short bundle (-xvs, -Werror, -cCODE): only its head is kept, and a
    # code letter anywhere in it may mean the rest of the line is a script.
    return head, any(ch in _CODE_LETTERS for ch in word[1:])


def _segment(words: list[tuple[str, bool]]) -> list[str]:
    """The words kept from one command segment (between separators).

    Kept: ``VAR=`` names, the program (and the program a wrapper or a
    runner such as ``uv run`` starts), flags without values, the program's
    known subcommands, and words that look like paths. Dropped: the word
    after any flag (it may be the flag's value: ``-p hunter2``) and every
    other positional word. The segment ends at the first quoted, odd or
    code-carrying word.
    """
    kept: list[str] = []
    program: str | None = None
    expect_program = True
    skip_value = False
    for word, quoted in words:
        if quoted or _ODD_DASH.search(word):
            break
        if skip_value:
            skip_value = False
            continue
        if program is not None and word.startswith("-"):
            flag, stop = _flag(word)
            if flag is not None:
                kept.append(flag)
            if stop:
                break
            if flag == "-m" and program.startswith("python"):
                expect_program = True
            else:
                skip_value = "=" not in word
            continue
        if expect_program and (env := _ENV_PREFIX.match(word)):
            kept.append(env.group(1) + "=")
            continue
        if not _plain_word(word):
            break
        if expect_program:
            kept.append(word)
            program = word.rsplit("/", 1)[-1].lower()
            if program in _SAY_PROGRAMS:
                break
            expect_program = program in _WRAPPERS
        elif program is not None and word in _SUBCOMMANDS.get(program, ()):
            kept.append(word)
            expect_program = (program, word) in _RUNS_PROGRAM
        elif _FILE_LIKE.fullmatch(word):
            kept.append(word)
    return kept


def command_skeleton(command: str) -> str:
    """The argv skeleton of a command line, as prompt-only mode sends it.

    Only the first line counts (split as ``str.splitlines`` splits, so a lone
    CR or U+2028 can't hide a second one). Each segment is cut down as
    ``_segment`` says; a redirection drops the rest of its segment. An
    unclosed quote keeps only the program name.
    """
    lines = command.replace("\x00", "").splitlines()
    line = lines[0] if lines else ""
    tokens = _tokenize(line)
    if tokens is None:
        first = line.split(maxsplit=1)
        return first[0] if first and _plain_word(first[0]) else ""
    out: list[str] = []
    words: list[tuple[str, bool]] = []
    redirected = False
    for text, is_op, quoted in [*tokens, (";", True, False)]:
        if not is_op:
            if not redirected:
                words.append((text, quoted))
        elif text in _SEPARATORS:
            kept = _segment(words)
            if kept:
                out += [*kept, text]
            words, redirected = [], False
        else:
            redirected = True
    if out:
        out.pop()  # the closing separator added above, or a trailing one
    return " ".join(out)[:MAX_SKELETON_CHARS].rstrip()


def prompt_only_test_command(command: str | None) -> str | None:
    """The test command as prompt-only mode sends it: whole, or not at all.

    The server matches tool runs against the stored test command, so a
    skeleton that lost words would match runs the command never ran. A
    one-word command is already recognized by the server's test patterns.
    """
    if command is None:
        return None
    skeleton = command_skeleton(command)
    if skeleton != command or len(skeleton.split()) < 2:
        return None
    return skeleton[:MAX_TEST_COMMAND_CHARS]


def prompt_only_skills(skills: list[str]) -> list[str]:
    """Skill names safe to send in prompt-only mode (plain names only)."""
    return [s for s in skills if len(s) <= MAX_SKILL_CHARS and _SKILL.fullmatch(s)]
