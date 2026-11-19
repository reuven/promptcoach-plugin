"""The Claude Code adapter: hook payloads in, hook output out.

Everything that is true only of Claude Code lives here: its event names and
payload fields (verified on Claude Code 2.1.326, docs/verified-claude-code.md
in the server repo), its ``CLAUDE_PLUGIN_*`` environment, its settings files,
and its output fields. Output is only ever ``systemMessage`` (shown to the
person, not to Claude) or, before a prompt, ``decision: block``.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from . import __version__, core, gitdiff
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
    command_without_token_message=(  # noqa: S106 - a message, not a secret
        "Coach: that command wasn't applied, because the coach isn't set up yet. "
        "Add your token with /plugin → promptcoach → Configure, then send it again."
    ),
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


def turn_ended(payload: Mapping[str, Any], env: Mapping[str, str]) -> core.TurnEnded:
    """A ``Stop`` payload as the neutral event."""
    reply = payload.get("last_assistant_message")
    cwd = Path(_text(payload, "cwd"))
    return core.TurnEnded(
        session_id=_text(payload, "session_id"),
        prompt_id=_text(payload, "prompt_id"),
        cwd=cwd,
        project_dir=Path(env.get("CLAUDE_PROJECT_DIR") or cwd),
        reply=reply if isinstance(reply, str) else None,
        continuing=payload.get("stop_hook_active") is True,
    )


def _drop_coach(line: str) -> str:
    """The line without a leading "Coach: ", its first letter capitalized."""
    if not line.startswith("Coach: "):
        return line
    rest = line[len("Coach: ") :]
    return rest[:1].upper() + rest[1:]


def frame(message: str) -> str:
    """The message as Claude Code should show it: cleaned, marked ``[Coach]``.

    Claude Code puts its own label before a hook's message ("Stop says: "),
    and "Stop says: Coach: ..." read as if Stop were the coach. So the
    message starts with ``[Coach]`` and every later line is indented under
    it; the "Coach: " that starts each of the server's lines is dropped,
    with the sentence after it capitalized. (1.0.0b5's rules above and below
    looked heavier than the one line they framed.) The server's text stays
    agent-neutral; this framing is Claude Code's alone, and stays within
    the 4 KB cap.
    """
    first, *rest = [_drop_coach(line) for line in core.clean_message(message).split("\n")]
    text = "\n".join([f"[Coach] {first}", *(f"  {line}" if line else "" for line in rest)])
    return text.encode("utf-8")[: core.MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore")


def emit(reply: core.Reply | None, stdout: TextIO, *, allow_block: bool) -> None:
    """Write the reply as Claude Code hook output, or nothing at all.

    Only ``systemMessage`` (shown to the person, kept from Claude) and,
    where ``allow_block``, ``decision: block``. Never plain text: on
    ``UserPromptSubmit`` that would go into Claude's context. A block on
    ``Stop`` would make Claude keep going, so it is never allowed there.
    The message loses control characters (no terminal escapes) and is
    capped at 4 KB.

    A block also sets ``suppressOriginalPrompt``, so Claude Code's block
    message doesn't end with "Original prompt:" and the command echoed back.
    Claude Code versions that predate the key ignore it and still block
    (checked on 2.1.0, 2.1.142, 2.1.233 and 2.1.328).
    """
    if reply is None or reply.message is None:
        return
    message = frame(reply.message)
    out: dict[str, object]
    if reply.block and allow_block:
        out = {
            "decision": "block",
            "reason": message,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "suppressOriginalPrompt": True,
            },
        }
    else:
        out = {"systemMessage": message}
    stdout.write(json.dumps(out))
    stdout.flush()


def main(
    argv: list[str],
    stdin: TextIO,
    stdout: TextIO,
    env: Mapping[str, str],
    started: float | None = None,
) -> int:
    """Run one hook: ``before``, ``record`` or ``after``. Always returns 0.

    Nothing here may break the person's session, so any failure, ours or
    the server's, ends silently, except for a coach command: it is spotted
    first, from the raw prompt, and if anything after that fails it is
    blocked (``COMMAND_NOT_ANSWERED``) rather than handed to Claude.
    """
    command = False
    written: list[bool] = []
    try:
        mode = argv[0] if argv else ""
        payload = json.loads(stdin.read(MAX_STDIN_CHARS).lstrip("\ufeff"))
        if not isinstance(payload, dict):
            return 0
        prompt = payload.get("prompt")
        command = (
            mode == "before"
            and PROFILE.capabilities.can_block_prompt
            and isinstance(prompt, str)
            and core.is_coach_command(prompt)
        )
        config = config_from_env(env)
        gitdiff.install_cleanup()
        if mode == "before":
            reply = core.before(prompt_submitted(payload, env), config, PROFILE, started)
            written.append(True)
            emit(reply, stdout, allow_block=True)
        elif mode == "record":
            run = tool_ran(payload)
            if run is not None:
                core.record(run, config)
        elif mode == "after":
            emit(core.after(turn_ended(payload, env), config, PROFILE), stdout, allow_block=False)
    except Exception:  # noqa: BLE001 - a coach must never break the session
        if command and not written:
            with contextlib.suppress(Exception):
                emit(core.Reply(core.COMMAND_NOT_ANSWERED, True, True), stdout, allow_block=True)
    return 0
