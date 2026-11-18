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


def test_ci_runs_on_every_os_with_both_pythons() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "runs-on: ${{ matrix.os }}" in ci
    assert "os: [ubuntu-latest, windows-latest, macos-latest]" in ci
    assert 'python: ["3.9", "3.14"]' in ci
    uses = re.findall(r"uses: (\S+)", ci)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses)


def test_every_version_string_is_one_value() -> None:
    """The plugin's version is the server's, released together (README)."""
    from promptcoach_client import __version__

    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    pyproject = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M)
    assert pyproject is not None
    versions = {plugin["version"], pyproject.group(1), __version__}
    versions |= {p["version"] for p in market["plugins"] if "version" in p}
    assert versions == {"1.0.0b1"}


def _ci_jobs() -> dict[str, str]:
    """ci.yml's jobs, each as its block of text (no YAML parser in the stdlib)."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    jobs = ci.split("\njobs:\n", 1)[1]
    names = re.findall(r"^  ([\w-]+):$", jobs, re.M)
    blocks = re.split(r"^  [\w-]+:$", jobs, flags=re.M)[1:]
    return dict(zip(names, blocks))


def test_ci_gates_coverage_on_the_union_of_every_cell() -> None:
    """Each cell keeps its coverage data and fails only on a failed test; one
    job combines every cell's data and holds the union to 100%, so each
    platform's branches must be covered by tests that ran on that platform."""
    jobs = _ci_jobs()
    assert set(jobs) == {"check", "coverage"}
    check, union = jobs["check"], jobs["coverage"]
    # Every cell runs one pytest command, the same everywhere, keeping its data
    # under its own name and uploading it even when a test failed.
    assert "COVERAGE_FILE: .coverage.${{ matrix.os }}-${{ matrix.python }}" in check
    [pytest_step] = re.findall(r"- run: (uv run pytest.*)", check)
    assert pytest_step == "uv run pytest --cov-fail-under=0"
    assert "runner.os" not in check and "matrix.os ==" not in check
    assert "continue-on-error" not in check
    assert re.search(r"actions/upload-artifact@[0-9a-f]{40}", check)
    assert "name: coverage-${{ matrix.os }}-${{ matrix.python }}" in check
    assert "include-hidden-files: true" in check
    assert "if-no-files-found: error" in check
    # The union: after every cell, even a failed one; it fails if any did.
    assert "needs: check" in union
    assert "if: ${{ !cancelled() }}" in union
    assert re.search(r"actions/download-artifact@[0-9a-f]{40}", union)
    assert "uv run coverage combine" in union
    assert "uv run coverage report --fail-under=100" in union
    assert "needs.check.result != 'success'" in union
    # The threshold lives only in the union (and pyproject, for local runs).
    assert re.findall(r"fail-under=(\d+)", check + union) == ["0", "100"]


def test_coverage_paths_map_every_runner_to_the_repo() -> None:
    """Windows and POSIX data name the same files (relative, either slash)."""
    from coverage.files import PathAliases

    pyproject = (ROOT / "pyproject.toml").read_text()
    assert re.search(r"^relative_files = true$", pyproject, re.M)
    block = re.search(r"\[tool\.coverage\.paths\]\nsource = \[(.*?)\]", pyproject, re.S)
    assert block is not None
    patterns = ast.literal_eval("[" + block.group(1) + "]")
    aliases = PathAliases(relative=True)
    for pattern in patterns[1:]:
        aliases.add(pattern, patterns[0])
    want = "plugins/promptcoach/hooks/coach_hook.py"
    for seen in [
        "plugins/promptcoach/hooks/coach_hook.py",
        "plugins\\promptcoach\\hooks\\coach_hook.py",
        "D:\\a\\promptcoach-plugin\\promptcoach-plugin\\plugins\\promptcoach\\hooks\\coach_hook.py",
        "/home/runner/work/promptcoach-plugin/promptcoach-plugin/plugins/promptcoach/hooks/coach_hook.py",
        "/Users/runner/work/x/x/plugins/promptcoach/hooks/coach_hook.py",
    ]:
        assert aliases.map(seen).replace("\\", "/") == want, seen


def test_local_runs_keep_the_100_percent_gate() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "--cov-fail-under=100" in pyproject
