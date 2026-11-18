"""The client's copies of server values match the server (or its pinned copy)."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from conftest import git
from promptcoach_client import contract as c
from promptcoach_client import core, forbidden
from promptcoach_client.claude_code import PROFILE
from server_contract import contract, pinned, read_server, server_repo

SERVER = contract()


def test_pin_matches_server_when_available() -> None:
    repo = server_repo()
    if repo is None:
        pytest.skip("server repo not checked out here; the pinned copy is used")
    assert read_server(repo) == pinned()


def test_agents_and_files() -> None:
    assert sorted(c.AGENTS) == SERVER["agents"]
    assert list(c.PROJECT_INSTRUCTION_FILES) == SERVER["project_instruction_files"]
    assert list(c.GLOBAL_INSTRUCTION_FILES) == SERVER["global_instruction_files"]
    assert SERVER["max_instruction_file_chars"] == c.MAX_INSTRUCTION_FILE_CHARS
    assert SERVER["max_instructions_total_chars"] == c.MAX_INSTRUCTIONS_TOTAL_CHARS


def test_setup_vocabulary() -> None:
    assert sorted(c.KNOWN_HOOK_TOOLS) == SERVER["known_hook_tools"]
    assert sorted(c.HOOK_MOMENTS) == SERVER["hook_moments"]
    assert sorted(c.PROJECT_SIGNALS) == SERVER["project_signals"]
    assert sorted(c.LINTERS) == SERVER["linters"]


def test_claude_code_profile_speaks_the_contract() -> None:
    assert PROFILE.agent in SERVER["agents"]
    assert PROFILE.sources.hook_tools_keys == set(SERVER["hook_events"])
    # The files the server's claude-code profile reads (agents.py).
    assert PROFILE.sources.project_instruction_files == (
        "CLAUDE.md",
        "AGENTS.md",
        "SYSTEM.md",
        ".claude/CLAUDE.md",
    )
    assert PROFILE.sources.global_instruction_files == (
        "~/.claude/CLAUDE.md",
        "~/.codex/AGENTS.md",
        "~/.pi/agent/AGENTS.md",
    )


def test_forbidden_lists_match_server() -> None:
    assert sorted(forbidden.FORBIDDEN_NAMES) == sorted(SERVER["forbidden_names"])
    assert list(forbidden.FORBIDDEN_SUFFIXES) == SERVER["forbidden_suffixes"]
    assert list(forbidden.FORBIDDEN_PATH_ENDINGS) == SERVER["forbidden_path_endings"]
    assert list(forbidden.ENV_TEMPLATE_SUFFIXES) == SERVER["env_template_suffixes"]


# What the server's is_forbidden_path answers for each path (pinned, since the
# server's module needs Python 3.11+; checked live below where it loads).
EXPECTED = {
    ".env": True, "a/b/.ENV": True, ".envrc": True, ".env.local": True,
    ".env.example": False, "x/.env.sample": False, "prod.env": True,
    "credentials.json": True, ".netrc": True, ".pypirc": True, ".npmrc": True,
    ".pgpass": True, ".htpasswd": True, ".git-credentials": True,
    "conf/secrets.toml": True, "k.pem": True, "K.KEY": True, "a.p12": True,
    "a.pfx": True, "a.jks": True, "a.keystore": True, "main.tfvars": True,
    "main.tfvars.json": True, "id_rsa": True, "id_rsa.pub": False,
    "id_ed25519_work": True, "keys/id_ecdsa": True, "id_dsa.pub": False,
    ".aws/credentials": True, "home/.docker/config.json": True, ".kube/config": True,
    "kube/config": False, "app.py": False, "README.md": False, "envfile": False,
    "environment.yml": False, "a\\b\\.env": True,
}  # fmt: skip
PATHS = list(EXPECTED)


def _server_check() -> Callable[[str], bool] | None:
    """The server's own function, where its repo is here and it loads."""
    repo = server_repo()
    if repo is None:
        return None
    spec = importlib.util.spec_from_file_location(
        "server_diffparse", repo / "src/promptcoach/diffparse.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except re.error:  # its regexes need Python 3.11+
        return None
    check: Callable[[str], bool] = module.is_forbidden_path
    return check


@pytest.mark.parametrize("path", PATHS)
def test_is_forbidden_path_agrees_with_server(path: str) -> None:
    assert forbidden.is_forbidden_path(path) is EXPECTED[path]
    server = _server_check()
    if server is not None:
        assert server(path) is EXPECTED[path]


def test_pathspecs_exclude_every_forbidden_path(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    names = [p for p in PATHS if "\\" not in p]
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x\n")

    git(tmp_path, "add", "-A", "--", ".", *forbidden.exclude_pathspecs())
    added = set(git(tmp_path, "ls-files").splitlines())

    for name in names:
        if forbidden.is_forbidden_path(name):
            assert name not in added, name
    # Pathspecs can't keep templates, so they over-exclude: that's the safe side.
    assert {"app.py", "README.md", "envfile", "environment.yml", "kube/config"} <= added


def test_plugin_version_matches_manifest() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "plugins/promptcoach/.claude-plugin/plugin.json").read_text())
    pyproject = (root / "pyproject.toml").read_text()
    assert manifest["version"] == PROFILE.plugin_version
    assert re.search(r'^version = "(.+)"', pyproject, re.MULTILINE)[1] == PROFILE.plugin_version  # type: ignore[index]


# The server's version (feat/ci-fix, src/promptcoach/versions.py): 2 to 4
# release parts of 1 to 4 ASCII digits, an optional a/b/rc N, an optional
# .devN (each N 1 to 4 digits), at most 32 characters, matched in full.
SERVER_VERSION_PATTERN = (
    r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}"
    r"(?:(?:a|b|rc)[0-9]{1,4})?"
    r"(?:\.dev[0-9]{1,4})?"
)
SERVER_MAX_VERSION_CHARS = 32


def test_version_pattern_is_the_servers() -> None:
    assert (c.VERSION_PATTERN, c.MAX_VERSION_CHARS) == (
        SERVER_VERSION_PATTERN,
        SERVER_MAX_VERSION_CHARS,
    )


def test_version_pattern_matches_the_server_source_where_it_is_checked_out() -> None:
    repo = server_repo()
    source = repo / "src" / "promptcoach" / "versions.py" if repo else None
    if source is None or not source.is_file():
        pytest.skip("no server checkout with versions.py here; the copy above is used")
    names: dict[str, object] = {}
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            with contextlib.suppress(ValueError):
                names[node.targets[0].id] = ast.literal_eval(node.value)
    assert names["VERSION_PATTERN"] == SERVER_VERSION_PATTERN
    assert names["MAX_VERSION_CHARS"] == SERVER_MAX_VERSION_CHARS


@pytest.mark.parametrize(
    "version",
    [
        "1.0",
        "1.0.0",
        "1.0.0b1",
        "1.0.0a12",
        "2.1rc3",
        "1.0.0.dev4",
        "1.0.0b1.dev2",
        "0.1.0",
        "9999.9999.9999.9999",
        "9999.9999.9999.9999b9999.dev9999",  # 32 characters
    ],
)
def test_versions_the_server_accepts(version: str) -> None:
    assert core.valid_version(version)


@pytest.mark.parametrize(
    "version",
    [
        "1",
        "1.0.0.0.0",
        "10000.0",
        "1.0.0-beta",
        "1.0.0beta1",
        "1.0.0c1",
        "1.0.0b",
        "1.0.0b12345",
        "1.0.0.dev",
        "1.0.0b1.post1",
        "v1.0.0",
        "1.0.0 ",
        "1.0.0\n",
        "\u0661.0",
        "1.0+local",
        "9999.9999.9999.9999rc9999.dev9999",  # 33 characters
        "",
    ],
)
def test_versions_the_server_refuses(version: str) -> None:
    assert not core.valid_version(version)


@given(st.from_regex(SERVER_VERSION_PATTERN, fullmatch=True))
def test_every_version_the_client_sends_is_pep_440(version: str) -> None:
    """The server orders versions by PEP 440, so each one the client accepts
    must parse as one (packaging, which pytest brings, is the oracle; the
    client itself never orders versions)."""
    from packaging.version import Version

    assert core.valid_version(version) == (len(version) <= SERVER_MAX_VERSION_CHARS)
    Version(version)
