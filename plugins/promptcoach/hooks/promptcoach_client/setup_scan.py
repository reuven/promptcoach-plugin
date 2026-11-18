"""The project's agent setup, as the coach sees it: names and flags, no code.

Why: the coach judges guidance layers (instructions, tests, hooks, CI)
without ever receiving the repository. Everything here reads a handful of
well-known files and reports what they contain in the server's vocabulary
(``SetupSnapshot``). Hook commands are reduced to the known tool names they
run; ``.env`` and other credential files are never opened.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import (
    HOOK_EVENT_PATTERN,
    KNOWN_HOOK_TOOLS,
    LINTERS,
    MAX_HOOK_EVENTS,
    MAX_INSTRUCTION_FILE_CHARS,
    MAX_INSTRUCTIONS_TOTAL_CHARS,
    MAX_SKILL_CHARS,
    MAX_SKILLS,
    PROJECT_SIGNALS,
)
from .forbidden import is_forbidden_path
from .skeleton import prompt_only_skills, prompt_only_test_command

MAX_CONFIG_BYTES = 1_000_000  # pyproject, settings, Makefile: read no more
MAX_WALK_FILES = 2_000  # files looked at when searching for notebooks
MAX_WALK_DEPTH = 3
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages"})

_HOOK_EVENT = re.compile(HOOK_EVENT_PATTERN)
_ENV_LINES = frozenset({".env", ".env*", "*.env", "/.env", "/.env*"})
_DATA_DIR_LINES = frozenset({"data", "data/", "/data", "/data/", "data/*", "/data/*"})
_DATA_EXTENSIONS = ("csv", "tsv", "parquet", "xlsx", "dta", "sav")
_PLAIN_TOOLS = sorted(t for t in KNOWN_HOOK_TOOLS if ":" not in t and t[:5] != "ruff-")
_TOOL_WORD = {t: re.compile(rf"(?<![\w.-]){re.escape(t)}(?![\w-])") for t in _PLAIN_TOOLS}
_RUFF_SUB = re.compile(r"(?<![\w.-])ruff\s+(check|format)(?![\w-])")
_MARKER = re.compile(r"#(pc:[a-z_]+)")
_DEPENDENCY_NAMES = {name: name for name in PROJECT_SIGNALS - {"jupyter", "sklearn"}}
_DEPENDENCY_NAMES["scikit-learn"] = "sklearn"
_DEPENDENCY_NAMES["sklearn"] = "sklearn"
_DEPENDENCY_NAMES["jupyter"] = "jupyter"
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PYPROJECT_DEP = re.compile(r"""["']\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?=[\[<>=!~;,"'\s])""")
_PYPROJECT_LINTERS = {
    "ruff": "[tool.ruff",
    "mypy": "[tool.mypy",
    "pyright": "[tool.pyright",
    "black": "[tool.black",
    "pylint": "[tool.pylint",
}
_LINTER_FILES = {
    "ruff": ("ruff.toml", ".ruff.toml"),
    "mypy": ("mypy.ini", ".mypy.ini"),
    "pyright": ("pyrightconfig.json",),
    "black": (),
    "flake8": (".flake8",),
    "pylint": (".pylintrc", "pylintrc"),
}
_SETUP_CFG_LINTERS = {"mypy": "[mypy", "flake8": "[flake8", "pylint": "[pylint"}


@dataclass(frozen=True)
class SetupSources:
    """Where one agent keeps the things the snapshot reports.

    The core doesn't know any agent's layout; the adapter says which
    instruction files its agent reads, where its hook settings live (``~/``
    means the home directory), which hook event names the server accepts as
    ``hook_tools`` keys, and where skills are.
    """

    project_instruction_files: tuple[str, ...]
    global_instruction_files: tuple[str, ...]
    hook_settings_files: tuple[str, ...]
    hook_tools_keys: frozenset[str]
    skills_dirs: tuple[str, ...]


def contained(path: Path, root: Path) -> Path | None:
    """``path``'s real location, if it stays inside ``root`` and isn't a credential file.

    Why: a project could make ``CLAUDE.md`` (or ``.claude/``) a symlink to
    ``~/.ssh/id_rsa``; reading through it would send that file. Symlinks
    are allowed only when they end inside the same tree (project files in
    the project, global files in the home directory), and never at a
    forbidden file.
    """
    try:
        real = path.resolve(strict=True)
        base = root.resolve(strict=True)
    except (OSError, RuntimeError):  # missing, or a symlink loop
        return None
    if real != base and base not in real.parents:
        return None
    return None if is_forbidden_path(real.relative_to(base).as_posix()) else real


def read_text(path: Path, root: Path, limit: int = MAX_CONFIG_BYTES) -> str | None:
    """A regular file's text (at most ``limit`` characters), or None.

    Only inside ``root`` (see ``contained``), opened without following a
    symlink swapped in meanwhile, and only if it is a regular file (never a
    FIFO or device). Never raises: anything unreadable is just absent.
    """
    real = contained(path, root)
    if real is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(real, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit * 4).decode("utf-8", errors="replace")[:limit]
    except OSError:
        return None
    finally:
        os.close(fd)


def _resolve(name: str, project_dir: Path, home: Path) -> tuple[Path, Path]:
    """A ``~/``-relative or project-relative file name, with the tree it must stay in."""
    return (home / name[2:], home) if name.startswith("~/") else (project_dir / name, project_dir)


def _instructions(project_dir: Path, home: Path, sources: SetupSources) -> dict[str, str]:
    found: dict[str, str] = {}
    room = MAX_INSTRUCTIONS_TOTAL_CHARS
    for name in (*sources.project_instruction_files, *sources.global_instruction_files):
        text = read_text(*_resolve(name, project_dir, home), MAX_INSTRUCTION_FILE_CHARS)
        if text is not None and room > 0:
            found[name] = text[:room]
            room -= len(found[name])
    return found


def _ignore_lines(project_dir: Path) -> set[str] | None:
    text = read_text(project_dir / ".gitignore", project_dir)
    if text is None:
        return None
    return {line.strip() for line in text.splitlines()}


def gitignore_covers_env(lines: set[str] | None) -> bool | None:
    """Whether ``.gitignore`` keeps ``.env`` out of git; None without one."""
    return None if lines is None else bool(lines & _ENV_LINES)


def gitignore_covers_data(lines: set[str] | None) -> bool | None:
    """Whether ``.gitignore`` covers ``data/`` and every data file extension."""
    if lines is None:
        return None
    exts = all({f"*.{e}", f"/*.{e}", f"**/*.{e}"} & lines for e in _DATA_EXTENSIONS)
    return exts and bool(lines & _DATA_DIR_LINES)


def _test_command(project_dir: Path, instructions: dict[str, str], pyproject: str) -> str | None:
    text = "\n".join(instructions.values())
    if "uv run pytest" in text:
        return "uv run pytest"
    if re.search(r"(?<![\w-])pytest(?![\w-])", text):
        return "pytest"
    if "[tool.pytest" in pyproject:
        return "uv run pytest" if (project_dir / "uv.lock").is_file() else "pytest"
    makefile = read_text(project_dir / "Makefile", project_dir) or ""
    if re.search(r"^test\s*:", makefile, re.MULTILINE):
        return "make test"
    return None


def _linters(project_dir: Path, pyproject: str) -> list[str]:
    setup_cfg = (read_text(project_dir / "setup.cfg", project_dir) or "") + (
        read_text(project_dir / "tox.ini", project_dir) or ""
    )
    found = {name for name, section in _PYPROJECT_LINTERS.items() if section in pyproject}
    found |= {
        name
        for name, files in _LINTER_FILES.items()
        if any((project_dir / f).is_file() for f in files)
    }
    found |= {name for name, section in _SETUP_CFG_LINTERS.items() if section in setup_cfg}
    return sorted(found & LINTERS)


def _hook_commands(entries: object) -> Iterator[str]:
    """Every hook command line under one event, args joined in."""
    for entry in entries if isinstance(entries, list) else []:
        hooks = entry.get("hooks") if isinstance(entry, dict) else None
        for hook in hooks if isinstance(hooks, list) else []:
            if not isinstance(hook, dict):
                continue
            parts = [hook.get("command")]
            args = hook.get("args")
            parts += args if isinstance(args, list) else []
            yield " ".join(p for p in parts if isinstance(p, str))


def tools_in(command: str) -> set[str]:
    """The known tool names a hook command runs, never the command itself."""
    found = {tool for tool, word in _TOOL_WORD.items() if word.search(command)}
    found |= {f"ruff-{sub}" for sub in _RUFF_SUB.findall(command)}
    found |= {m for m in _MARKER.findall(command) if m in KNOWN_HOOK_TOOLS}
    return found


def _hooks(
    project_dir: Path, home: Path, sources: SetupSources
) -> tuple[list[str], dict[str, list[str]]]:
    events: set[str] = set()
    tools: dict[str, set[str]] = {}
    for name in sources.hook_settings_files:
        try:
            settings: Any = json.loads(read_text(*_resolve(name, project_dir, home)) or "{}")
        except ValueError:
            continue
        hooks = settings.get("hooks") if isinstance(settings, dict) else None
        for event, entries in (hooks if isinstance(hooks, dict) else {}).items():
            if not _HOOK_EVENT.fullmatch(event):
                continue
            events.add(event)
            if event in sources.hook_tools_keys:
                for command in _hook_commands(entries):
                    tools.setdefault(event, set()).update(tools_in(command))
    hook_tools = {event: sorted(names) for event, names in sorted(tools.items()) if names}
    return sorted(events)[:MAX_HOOK_EVENTS], hook_tools


def _skills(project_dir: Path, sources: SetupSources) -> list[str]:
    names: list[str] = []
    for rel in sources.skills_dirs:
        top = contained(project_dir / rel, project_dir)
        try:
            listed = sorted(top.iterdir()) if top else []
        except OSError:
            continue
        # Each skill must be a directory that stays inside the project too.
        entries = [p.name for p in listed if (c := contained(p, project_dir)) and c.is_dir()]
        names += [n for n in entries if len(n) <= MAX_SKILL_CHARS and n not in names]
    return names[:MAX_SKILLS]


def _walk(project_dir: Path) -> Iterator[Path]:
    """Files near the top of the project, skipping hidden and vendored trees."""
    seen = 0
    for root, dirs, files in os.walk(project_dir):
        depth = len(Path(root).relative_to(project_dir).parts)
        dirs[:] = (
            []
            if depth >= MAX_WALK_DEPTH
            else [d for d in dirs if d[0] != "." and d not in _SKIP_DIRS]
        )
        for name in files:
            seen += 1
            if seen > MAX_WALK_FILES:
                return
            yield Path(root) / name


def _is_marimo(path: Path, project_dir: Path) -> bool:
    head = read_text(path, project_dir, 2_000) or ""
    return "import marimo" in head and "marimo.App" in head


def _project_signals(project_dir: Path, pyproject: str) -> list[str]:
    names = [m.group(1) for m in _PYPROJECT_DEP.finditer(pyproject)]
    for req in sorted(project_dir.glob("requirements*.txt")):
        for line in (read_text(req, project_dir) or "").splitlines():
            m = _REQUIREMENT.match(line)
            names += [m.group(1)] if m and not line.lstrip().startswith("#") else []
    found = {_DEPENDENCY_NAMES[n.lower()] for n in names if n.lower() in _DEPENDENCY_NAMES}
    for path in _walk(project_dir):
        if path.suffix == ".ipynb":
            found.add("jupyter")
        elif path.suffix == ".py" and "marimo" not in found and _is_marimo(path, project_dir):
            found.add("marimo")
    return sorted(found & PROJECT_SIGNALS)


def collect_setup(
    project_dir: Path, home: Path, sources: SetupSources, *, prompt_only: bool = False
) -> dict[str, object]:
    """The ``SetupSnapshot`` for ``project_dir``, ready to send.

    Use it once per prompt. In prompt-only mode the test command is sent only
    when it is its own skeleton, and skills only when they are plain names.
    Instruction files are sent in both modes: they are the setup the person
    asked to be coached on.
    """
    pyproject = read_text(project_dir / "pyproject.toml", project_dir) or ""
    instructions = _instructions(project_dir, home, sources)
    ignore = _ignore_lines(project_dir)
    hook_events, hook_tools = _hooks(project_dir, home, sources)
    test_command = _test_command(project_dir, instructions, pyproject)
    skills = _skills(project_dir, sources)
    if prompt_only:
        test_command = prompt_only_test_command(test_command)
        skills = prompt_only_skills(skills)
    return {
        "instructions": instructions,
        "gitignore_covers_env": gitignore_covers_env(ignore),
        "has_tests_dir": any((project_dir / d).is_dir() for d in ("tests", "test")),
        "test_command": test_command,
        "has_ci": any((project_dir / ".github" / "workflows").glob("*.y*ml")),
        "has_precommit": (project_dir / ".pre-commit-config.yaml").is_file(),
        "linters": _linters(project_dir, pyproject),
        "hook_events": hook_events,
        "hook_tools": hook_tools,
        "project_signals": _project_signals(project_dir, pyproject),
        "gitignore_covers_data": gitignore_covers_data(ignore),
        "skills": skills,
    }
