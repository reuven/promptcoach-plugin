"""The server's contract values, read from the server repo or from a pinned copy.

The server repo is private, so CI and other machines use
``tests/pinned/server_contract.json``. Where the server repo is checked out
(``PROMPTCOACH_SERVER_REPO``, or ``../PromptCoach-v1`` next to this repo),
the values are read from its source with ``ast`` (nothing is imported or
run) and the pin itself is checked against them, so it can't go stale.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "tests" / "pinned" / "server_contract.json"


def server_repo() -> Path | None:
    """The server checkout, if this machine has one."""
    path = Path(os.environ.get("PROMPTCOACH_SERVER_REPO") or ROOT.parent / "PromptCoach-v1")
    return path if (path / "src" / "promptcoach" / "schemas.py").is_file() else None


def _literal_args(node: ast.expr) -> list[str]:
    """The strings of ``Literal[...]`` (or a union of them)."""
    if isinstance(node, ast.Subscript):
        inner = node.slice
        items = inner.elts if isinstance(inner, ast.Tuple) else [inner]
        return [i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)]
    raise ValueError("not a Literal")


def _assignments(path: Path) -> dict[str, ast.expr]:
    tree = ast.parse(path.read_text())
    found: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            found[node.target.id] = node.value
            found[f"{node.target.id}:annotation"] = node.annotation
    return found


def _strings(node: ast.expr) -> list[str]:
    value = ast.literal_eval(node.args[0] if isinstance(node, ast.Call) else node)
    return sorted(value) if isinstance(value, (set, frozenset)) else list(value)


def _joined(node: ast.expr, names: dict[str, ast.expr]) -> str:
    """A string built from literals and f-string references to string constants."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name):
        return _joined(names[node.value.id], names)
    if isinstance(node, ast.JoinedStr):
        return "".join(_joined(part, names) for part in node.values)
    raise ValueError("not a plain string")


def _flags(node: ast.expr) -> list[str]:
    """The ``re`` flag names of ``re.A | re.B``."""
    if isinstance(node, ast.BinOp):
        return sorted(_flags(node.left) + _flags(node.right))
    if isinstance(node, ast.Attribute):
        return [node.attr]
    raise ValueError("not a flag")


def read_server(repo: Path) -> dict[str, Any]:
    """The contract values, parsed from the server's source."""
    src = repo / "src" / "promptcoach"
    schemas = _assignments(src / "schemas.py")
    diffparse = _assignments(src / "diffparse.py")
    rules = _assignments(src / "analysis" / "rules.py")
    command_re = rules["COMMAND_RE"]
    assert isinstance(command_re, ast.Call)
    snapshot = next(
        n
        for n in ast.walk(ast.parse((src / "schemas.py").read_text()))
        if isinstance(n, ast.ClassDef) and n.name == "SetupSnapshot"
    )
    linters = next(
        s.annotation
        for s in snapshot.body
        if isinstance(s, ast.AnnAssign)
        and isinstance(s.target, ast.Name)
        and s.target.id == "linters"
    )
    assert isinstance(linters, ast.Subscript)
    return {
        "agents": sorted(_literal_args(schemas["Agent"])),
        "project_instruction_files": _strings(schemas["PROJECT_INSTRUCTION_FILES"]),
        "global_instruction_files": _strings(schemas["GLOBAL_INSTRUCTION_FILES"]),
        "max_instruction_file_chars": ast.literal_eval(schemas["MAX_INSTRUCTION_FILE_CHARS"]),
        "max_instructions_total_chars": ast.literal_eval(schemas["MAX_INSTRUCTIONS_TOTAL_CHARS"]),
        "known_hook_tools": sorted(_literal_args(schemas["KnownHookTool"])),
        "hook_events": sorted(_literal_args(schemas["HookEvent"])),
        "hook_moments": sorted(_literal_args(schemas["HookMoment"])),
        "project_signals": sorted(_literal_args(schemas["ProjectSignal"])),
        "linters": sorted(_literal_args(linters.slice)),
        "forbidden_names": _strings(diffparse["_FORBIDDEN_NAMES"]),
        "forbidden_suffixes": _strings(diffparse["_FORBIDDEN_SUFFIXES"]),
        "forbidden_path_endings": _strings(diffparse["_FORBIDDEN_PATH_ENDINGS"]),
        "env_template_suffixes": _strings(diffparse["_ENV_TEMPLATE_SUFFIXES"]),
        # Which prompts are coach commands, the only ones a block may keep from the agent.
        "command_pattern": _joined(command_re.args[0], rules),
        "command_flags": _flags(command_re.args[1]),
        "command_synonyms": sorted(ast.literal_eval(rules["COMMAND_SYNONYMS"])),
    }


def pinned() -> dict[str, Any]:
    """The pinned copy of the contract values."""
    value: dict[str, Any] = json.loads(PINNED.read_text())
    return value


def contract() -> dict[str, Any]:
    """The live server's values where available, else the pinned copy."""
    repo = server_repo()
    return read_server(repo) if repo else pinned()
