"""The plugin's manifests and hook wiring, as verified on Claude Code 2.1.326."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "promptcoach"
CLIENT = PLUGIN / "hooks" / "promptcoach_client"


def test_user_config() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    config = manifest["userConfig"]

    assert manifest["name"] == "promptcoach"
    assert manifest["displayName"] == "Prompt coach"
    assert config["token"]["sensitive"] is True
    assert config["token"]["required"] is True
    assert config["token"]["description"] == "From coach.lernerpython.com/signin"
    assert config["server_url"]["default"] == "https://coach.lernerpython.com"
    assert config["prompt_only"] == {
        "type": "boolean",
        "title": "Prompt-only mode",
        "description": "Never send code or diffs",
        "default": False,
    }


def test_marketplace() -> None:
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "promptcoach"
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [
        ("promptcoach", "./plugins/promptcoach")
    ]


# Names only the Claude Code adapter may use: its events, env and output.
CLAUDE_CODE_NAMES = re.compile(
    r"UserPromptSubmit|PostToolUse|PreToolUse|\bStop\b|CLAUDE_|claude_code|systemMessage"
    r"|hookSpecificOutput|stop_hook_active|tool_response|\"decision\""
)


def test_core_never_names_claude_code() -> None:
    for path in sorted(CLIENT.glob("*.py")):
        if path.name == "claude_code.py":
            continue
        text = path.read_text()
        assert not CLAUDE_CODE_NAMES.search(text), (path.name, CLAUDE_CODE_NAMES.search(text))
        # The agent's wire name appears only in the server's list of agents.
        assert path.name == "contract.py" or "claude-code" not in text, path.name
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "claude_code" and "claude_code" not in [
                    a.name for a in node.names
                ]


def _is_stdlib(name: str) -> bool:
    """In the standard library (Python 3.9 has no ``sys.stdlib_module_names``)."""
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return name in names
    spec = importlib.util.find_spec(name)
    origin = spec.origin if spec else None
    stdlib = sysconfig.get_paths()["stdlib"]
    return origin in {"built-in", "frozen"} or (
        origin is not None and origin.startswith(stdlib) and "site-packages" not in origin
    )


def test_hook_code_is_stdlib_only() -> None:
    allowed_local = {"promptcoach_client", ""}
    for path in [*CLIENT.glob("*.py"), PLUGIN / "hooks" / "coach_hook.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                assert name in allowed_local or _is_stdlib(name), (
                    path,
                    name,
                )


def test_mit_license() -> None:
    """Reuven's decision (2026-11-17): MIT, named in every manifest."""
    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 Reuven M. Lerner" in text
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["license"] == "MIT"
    assert re.search(r'^license = "MIT"$', (ROOT / "pyproject.toml").read_text(), re.M)


def test_python_39_is_the_floor() -> None:
    """macOS ships Python 3.9 as /usr/bin/python3; the hook supports it."""
    import coach_hook

    assert coach_hook.MIN_PYTHON == (3, 9)
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.9"' in pyproject
    assert 'python_version = "3.9"' in pyproject
    assert 'target-version = "py39"' in pyproject
    assert 'python: ["3.9", "3.14"]' in (ROOT / ".github" / "workflows" / "ci.yml").read_text()
