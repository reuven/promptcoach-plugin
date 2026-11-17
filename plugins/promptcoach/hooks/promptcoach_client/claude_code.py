"""The Claude Code adapter: hook payloads in, hook output out.

Everything that is true only of Claude Code lives here: its event names and
payload fields (verified on Claude Code 2.1.326, docs/verified-claude-code.md
in the server repo), its ``CLAUDE_PLUGIN_*`` environment, its settings files,
and its output fields. Output is only ever ``systemMessage`` (shown to the
person, not to Claude) or, before a prompt, ``decision: block``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from . import __version__, core
from .setup_scan import SetupSources

MAX_STDIN_CHARS = 20_000_000
_EXIT_LINE = re.compile(r"Exit code (-?\d+)\s*")

PROFILE = core.AgentProfile(
    agent="claude-code",
    plugin_version=__version__,
    # Verified: a UserPromptSubmit block keeps the prompt from Claude; Stop
    # carries last_assistant_message; exit codes come from PostToolUse (0)
    # and PostToolUseFailure ("Exit code N").
    capabilities=core.Capabilities(can_block_prompt=True, has_reply=True, has_exit_codes=True),
    sources=SetupSources(
        project_instruction_files=("CLAUDE.md", "AGENTS.md", "SYSTEM.md", ".claude/CLAUDE.md"),
        global_instruction_files=(
            "~/.claude/CLAUDE.md",
            "~/.codex/AGENTS.md",
            "~/.pi/agent/AGENTS.md",
        ),
        hook_settings_files=(
            ".claude/settings.json",
            ".claude/settings.local.json",
            "~/.claude/settings.json",
        ),
        hook_tools_keys=frozenset({"PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit"}),
        skills_dirs=(".claude/skills", ".agents/skills"),
    ),
    missing_token_message="Prompt coach: add your token with /plugin → promptcoach → Configure.",  # noqa: S106
)


def config_from_env(env: Mapping[str, str]) -> core.Config:
    """The person's plugin settings (``userConfig``) and the plugin's data dir."""
    data = env.get("CLAUDE_PLUGIN_DATA")
    return core.Config(
        token=(env.get("CLAUDE_PLUGIN_OPTION_TOKEN") or "").strip() or None,
        server_url=env.get("CLAUDE_PLUGIN_OPTION_SERVER_URL") or core.DEFAULT_SERVER_URL,
        prompt_only=core.parse_bool(env.get("CLAUDE_PLUGIN_OPTION_PROMPT_ONLY")),
        data_dir=Path(data) if data else None,
        home=Path(env.get("HOME") or Path.home()),
    )


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(key)
    return value


def prompt_submitted(payload: Mapping[str, Any], env: Mapping[str, str]) -> core.PromptSubmitted:
    """A ``UserPromptSubmit`` payload as the neutral event."""
    cwd = Path(_text(payload, "cwd"))
    return core.PromptSubmitted(
        session_id=_text(payload, "session_id"),
        prompt_id=_text(payload, "prompt_id"),
        prompt=_text(payload, "prompt"),
        cwd=cwd,
        project_dir=Path(env.get("CLAUDE_PROJECT_DIR") or cwd),
    )


def tool_ran(payload: Mapping[str, Any]) -> core.ToolRan | None:
    """A Bash ``PostToolUse`` or ``PostToolUseFailure`` payload as a tool run.

    Success carries no exit code (it is 0) and one combined output stream in
    ``stdout``. Failure carries ``error``: ``Exit code N`` on its first line,
    then the output. An interrupted run, or a failure with no readable code,
    has exit code None.
    """
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if payload.get("tool_name") != "Bash" or not isinstance(command, str):
        return None
    prompt_id = payload.get("prompt_id")
    event = payload.get("hook_event_name")
    exit_code: int | None
    if event == "PostToolUse":
        response = payload.get("tool_response")
        response = response if isinstance(response, dict) else {}
        parts = [response.get("stdout"), response.get("stderr")]
        output = "\n".join(p for p in parts if isinstance(p, str) and p)
        exit_code = None if response.get("interrupted") is True else 0
    elif event == "PostToolUseFailure":
        error = payload.get("error")
        error = error if isinstance(error, str) else ""
        first, _, rest = error.partition("\n")
        match = _EXIT_LINE.fullmatch(first)
        output = rest if match else error
        exit_code = (
            int(match.group(1)) if match and payload.get("is_interrupt") is not True else None
        )
    else:
        return None
    return core.ToolRan(
        session_id=_text(payload, "session_id"),
        prompt_id=prompt_id if isinstance(prompt_id, str) else None,
        command=command,
        exit_code=exit_code,
        output=output,
    )


def turn_ended(payload: Mapping[str, Any]) -> core.TurnEnded:
    """A ``Stop`` payload as the neutral event."""
    reply = payload.get("last_assistant_message")
    return core.TurnEnded(
        session_id=_text(payload, "session_id"),
        prompt_id=_text(payload, "prompt_id"),
        cwd=Path(_text(payload, "cwd")),
        reply=reply if isinstance(reply, str) else None,
        continuing=payload.get("stop_hook_active") is True,
    )


def emit(reply: core.Reply | None, stdout: TextIO, *, allow_block: bool) -> None:
    """Write the reply as Claude Code hook output, or nothing at all.

    Only ``systemMessage`` (shown to the person, kept from Claude) and,
    where ``allow_block``, ``decision: block``. Never plain text: on
    ``UserPromptSubmit`` that would go into Claude's context. A block on
    ``Stop`` would make Claude keep going, so it is never allowed there.
    The message loses control characters (no terminal escapes) and is
    capped at 4 KB.
    """
    if reply is None or reply.message is None:
        return
    message = core.clean_message(reply.message)
    if reply.block and allow_block:
        out = {"decision": "block", "reason": message}
    else:
        out = {"systemMessage": message}
    stdout.write(json.dumps(out))
    stdout.flush()


def main(argv: list[str], stdin: TextIO, stdout: TextIO, env: Mapping[str, str]) -> int:
    """Run one hook: ``before``, ``record`` or ``after``. Always returns 0.

    Nothing here may break the person's session, so any failure, ours or
    the server's, ends silently.
    """
    try:
        mode = argv[0] if argv else ""
        payload = json.loads(stdin.read(MAX_STDIN_CHARS))
        if not isinstance(payload, dict):
            return 0
        config = config_from_env(env)
        if mode == "before":
            emit(
                core.before(prompt_submitted(payload, env), config, PROFILE),
                stdout,
                allow_block=True,
            )
        elif mode == "record":
            run = tool_ran(payload)
            if run is not None:
                core.record(run, config)
        elif mode == "after":
            emit(core.after(turn_ended(payload), config, PROFILE), stdout, allow_block=False)
    except Exception:  # noqa: BLE001, S110 - a coach must never break the session
        pass
    return 0
