"""The server's wire contract, mirrored for the client.

Why a module of its own: the server answers 422 to any unknown agent, file
name, hook tool or signal, and a 422 means the person gets no coaching at
all. These values copy ``promptcoach.schemas`` (server repo) and a test pins
them against it, so a drift fails here rather than silently in the field.
"""

from __future__ import annotations

AGENTS = frozenset(
    {
        "claude-code",
        "codex",
        "pi",
        "copilot-cli",
        "opencode",
        "antigravity-cli",
        "cursor-cli",
        "droid",
        "kiro-cli",
    }
)

PROJECT_INSTRUCTION_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "SYSTEM.md",
    ".claude/CLAUDE.md",
    "AGENTS.override.md",
    "APPEND_SYSTEM.md",
    ".github/copilot-instructions.md",
    "GEMINI.md",
)
GLOBAL_INSTRUCTION_FILES = (
    "~/.claude/CLAUDE.md",
    "~/.codex/AGENTS.md",
    "~/.pi/agent/AGENTS.md",
    "~/.codex/AGENTS.override.md",
    "~/.copilot/copilot-instructions.md",
    "~/.config/opencode/AGENTS.md",
    "~/.gemini/AGENTS.md",
    "~/.gemini/GEMINI.md",
)
MAX_INSTRUCTION_FILE_CHARS = 20_000
MAX_INSTRUCTIONS_TOTAL_CHARS = 140_000

KNOWN_HOOK_TOOLS = frozenset(
    {
        "ruff",
        "black",
        "mypy",
        "pyright",
        "pytest",
        "uv",
        "pre-commit",
        "prettier",
        "eslint",
        "pip",
        "ruff-check",
        "ruff-format",
        "pc:no_secrets_commit",
        "pc:block_pip",
        "pc:protect_paths",
        "pc:tests_before_done",
        "pc:format_after_edit",
        "pc:lint_after_edit",
        "pc:typecheck",
        "pc:no_data_commit",
    }
)
# The neutral moments the server accepts as ``hook_tools`` keys. It also
# accepts one agent's own event names, which only that agent's adapter knows.
HOOK_MOMENTS = frozenset({"before_prompt", "before_tool", "after_tool", "turn_end"})
PROJECT_SIGNALS = frozenset(
    {
        "pandas",
        "polars",
        "numpy",
        "marimo",
        "jupyter",
        "sklearn",
        "plotly",
        "matplotlib",
        "seaborn",
    }
)
LINTERS = frozenset({"ruff", "mypy", "pyright", "black", "flake8", "pylint"})

HOOK_EVENT_PATTERN = r"[A-Za-z][A-Za-z_]{2,39}"
MAX_HOOK_EVENTS = 30
MAX_SKILLS = 50
MAX_SKILL_CHARS = 80
MAX_TEST_COMMAND_CHARS = 200
ID_PATTERN = r"[A-Za-z0-9_.:-]{1,128}"
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+"

MAX_PROMPT_CHARS = 100_000
MAX_DIFF_CHARS = 300_000
MAX_TOOL_RUNS = 50
MAX_COMMAND_CHARS = 2_000
MAX_OUTPUT_CHARS = 4_000
MAX_REPLY_CHARS = 20_000
