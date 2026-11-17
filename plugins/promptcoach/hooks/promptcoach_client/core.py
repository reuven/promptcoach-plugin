"""The agent-neutral client: what happens before a prompt, after a tool run, and
at the end of a turn, for any agent.

An adapter parses its agent's hook payload into one of the neutral events
here (``PromptSubmitted``, ``ToolRan``, ``TurnEnded``), calls ``before``,
``record`` or ``after``, and turns the ``Reply`` into its agent's output.
This module never names an agent's events or output fields.

Promises kept here, whatever the agent:

- Silence on failure: any server or network problem means no reply, never
  an error in the person's session.
- HTTPS only, except to ``localhost``/``127.0.0.1``; no redirects followed.
  The token goes only in the ``Authorization`` header and is never written
  anywhere.
- Prompt-only mode, when the person or the server asked for it, or when the
  server's setting isn't known yet: no diff, no command output, commands as
  skeletons, no agent reply.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gitdiff
from .contract import (
    AGENTS,
    GLOBAL_INSTRUCTION_FILES,
    ID_PATTERN,
    MAX_COMMAND_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_PROMPT_CHARS,
    MAX_REPLY_CHARS,
    MAX_TOOL_RUNS,
    PROJECT_INSTRUCTION_FILES,
    VERSION_PATTERN,
)
from .setup_scan import SetupSources, collect_setup
from .skeleton import command_skeleton

DEFAULT_SERVER_URL = "https://coach.lernerpython.com"
BEFORE_HTTP_TIMEOUT_S = 1.8  # the server answers within 1.4 s; the hook gets 3 s
BEFORE_SNAPSHOT_S = 2.5  # the snapshot runs alongside the request
AFTER_HTTP_TIMEOUT_S = 12.0  # the hook gets 15 s
MAX_REPLY_BYTES = 256_000
STALE_AFTER_S = 24 * 3600
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
IS_TEST = re.compile(r"\b(pytest|unittest|tox|nox|make test|uv run pytest)\b")
_ID = re.compile(ID_PATTERN)
_VERSION = re.compile(VERSION_PATTERN)
_FILE_SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TEMP_PREFIXES = ("snap-", "idx-", "runs-")


@dataclass(frozen=True)
class Capabilities:
    """What the agent lets the client do and see (the server's flags)."""

    can_block_prompt: bool
    has_reply: bool
    has_exit_codes: bool


@dataclass(frozen=True)
class AgentProfile:
    """Everything agent-specific the core needs, supplied by the adapter."""

    agent: str
    plugin_version: str
    capabilities: Capabilities
    sources: SetupSources
    missing_token_message: str

    def __post_init__(self) -> None:
        """Refuse a profile the server would refuse (a 422 is silence)."""
        sources = self.sources
        if self.agent not in AGENTS or not _VERSION.fullmatch(self.plugin_version):
            raise ValueError("unknown agent or bad version")
        if not set(sources.project_instruction_files) <= set(PROJECT_INSTRUCTION_FILES):
            raise ValueError("unknown project instruction file")
        if not set(sources.global_instruction_files) <= set(GLOBAL_INSTRUCTION_FILES):
            raise ValueError("unknown global instruction file")
        if not self.missing_token_message:
            raise ValueError("missing_token_message is required")


@dataclass(frozen=True)
class Config:
    """The person's settings for the coach, as their agent passes them in."""

    token: str | None
    server_url: str
    prompt_only: bool  # the person's own setting: always wins
    data_dir: Path | None  # where the client keeps its state, if anywhere
    home: Path


@dataclass(frozen=True)
class PromptSubmitted:
    """The person sent a prompt; the agent hasn't seen it yet."""

    session_id: str
    prompt_id: str
    prompt: str
    cwd: Path
    project_dir: Path


@dataclass(frozen=True)
class ToolRan:
    """The agent ran a shell command. ``exit_code`` None: unknown or interrupted."""

    session_id: str
    prompt_id: str | None
    command: str
    exit_code: int | None
    output: str


@dataclass(frozen=True)
class TurnEnded:
    """The agent finished its turn."""

    session_id: str
    prompt_id: str
    cwd: Path
    reply: str | None  # the agent's final message, if the agent tells us
    continuing: bool  # a hook already made the agent go on: stay out of it


@dataclass(frozen=True)
class Reply:
    """What to show the person; ``block`` asks to keep the prompt from the agent."""

    message: str | None
    block: bool
    prompt_only: bool


def parse_bool(value: str | None) -> bool:
    """A setting's text as a boolean: true, 1, yes or on (any case)."""
    return (value or "").strip().lower() in {"true", "1", "yes", "on"}


def endpoint(server_url: str, path: str) -> str | None:
    """The URL for ``path`` on the coach server, or None if it isn't allowed.

    HTTPS only, except plain HTTP to this machine for local testing. No user
    name or password in the URL, no query or fragment.
    """
    try:
        url = urllib.parse.urlsplit(server_url.strip())
        host = url.hostname
    except ValueError:
        return None
    if not host or url.username or url.password or url.query or url.fragment:
        return None
    if url.scheme != "https" and not (url.scheme == "http" and host in LOCAL_HOSTS):
        return None
    return server_url.strip().rstrip("/") + path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: one could carry the token somewhere else."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def post(
    url: str | None, token: str, body: dict[str, Any], timeout: float
) -> dict[str, Any] | None:
    """POST ``body`` as JSON; the reply object, or None on any problem.

    ``timeout`` bounds the whole call (name lookup and a slow, trickling
    server included), not only each socket read: the request runs in a
    daemon thread that the hook simply stops waiting for.
    """
    if url is None:
        return None
    result: list[dict[str, Any]] = []

    def call() -> None:
        request = urllib.request.Request(  # noqa: S310 - endpoint() allows only http(s)
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "promptcoach-client",
            },
        )
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                raw = response.read(MAX_REPLY_BYTES + 1) if response.status == 200 else b""
            data = json.loads(raw) if 0 < len(raw) <= MAX_REPLY_BYTES else None
        except urllib.error.HTTPError as error:  # 4xx, 5xx, or a refused redirect
            error.close()
            return
        except (OSError, ValueError):  # refused, timed out, not JSON
            return
        if isinstance(data, dict):
            result.append(data)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(timeout)
    return result[0] if result else None


def parse_reply(data: dict[str, Any] | None) -> Reply | None:
    """The server's ``CoachReply``, checked; None if it isn't one."""
    if data is None:
        return None
    message = data.get("message")
    block = data.get("block_prompt", False)
    prompt_only = data.get("prompt_only")
    if not isinstance(prompt_only, bool) or not isinstance(block, bool):
        return None
    if message is not None and not isinstance(message, str):
        return None
    message = message or None
    if block and message is None:
        return None
    return Reply(message, block, prompt_only)


class Store:
    """The client's files in its data directory.

    ``state.json`` caches the server's prompt-only setting and which session
    was last told about a missing token. ``snap-*`` holds the start-of-turn
    snapshot, ``runs-*`` the turn's tool runs, ``idx-*`` a private git index.
    Session ids are used in file names only when plainly safe, else hashed.
    With no data directory every read is empty and every write is skipped.
    """

    def __init__(self, data_dir: Path | None) -> None:
        self.data_dir = data_dir

    def _path(self, prefix: str, session_id: str) -> Path | None:
        if self.data_dir is None:
            return None
        key = (
            session_id
            if _FILE_SAFE.fullmatch(session_id)
            else hashlib.sha256(session_id.encode()).hexdigest()[:32]
        )
        return self.data_dir / f"{prefix}{key}"

    def index_path(self, session_id: str) -> Path | None:
        """Where a private git index for this session may live."""
        return self._path("idx-", session_id)

    def _read_json(self, path: Path | None) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path else None
        except (OSError, ValueError):
            return None

    def _write_json(self, path: Path, value: object) -> None:
        """Write atomically, so a hook killed mid-write leaves the old file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)

    def _state(self) -> dict[str, Any]:
        state = self._read_json(self.data_dir / "state.json" if self.data_dir else None)
        return state if isinstance(state, dict) else {}

    def _update_state(self, **changes: object) -> None:
        if self.data_dir is not None:
            self._write_json(self.data_dir / "state.json", {**self._state(), **changes})

    def server_prompt_only(self) -> bool | None:
        """The server's prompt-only setting as last seen; None if never seen."""
        value = self._state().get("prompt_only")
        return value if isinstance(value, bool) else None

    def set_server_prompt_only(self, value: bool) -> None:
        """Remember the server's prompt-only setting for the next hook."""
        self._update_state(prompt_only=value)

    def first_warning(self, session_id: str) -> bool:
        """True the first time this is asked in a session (then remembered).

        Without a data directory nothing can be remembered, so the answer is
        False: better no warning than one on every prompt.
        """
        if self.data_dir is None or self._state().get("warned_session") == session_id:
            return False
        self._update_state(warned_session=session_id)
        return self._state().get("warned_session") == session_id

    def save_snapshot(self, session_id: str, prompt_id: str, tree: str) -> None:
        """Remember the start-of-turn tree for this session's current prompt."""
        path = self._path("snap-", session_id)
        if path is not None:
            self._write_json(path, {"prompt_id": prompt_id, "tree": tree})

    def take_snapshot(self, session_id: str, prompt_id: str) -> str | None:
        """The start-of-turn tree if it belongs to ``prompt_id``; always removed."""
        path = self._path("snap-", session_id)
        snap = self._read_json(path)
        if path is not None:
            path.unlink(missing_ok=True)
        if isinstance(snap, dict) and snap.get("prompt_id") == prompt_id:
            tree = snap.get("tree")
            return (
                tree if isinstance(tree, str) and re.fullmatch(r"[0-9a-f]{40,64}", tree) else None
            )
        return None

    def append_run(self, session_id: str, run: dict[str, object]) -> None:
        """Add one tool run to the session's runs file, in a single write.

        Hooks for parallel tool calls may append at once; one ``O_APPEND``
        write per line keeps their lines whole.
        """
        path = self._path("runs-", session_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(run) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def take_runs(self, session_id: str) -> list[dict[str, Any]]:
        """Read and remove the session's runs file (moved aside first, so a
        run recorded meanwhile starts a new file instead of being lost)."""
        path = self._path("runs-", session_id)
        if path is None:
            return []
        taken = path.with_name(f".taken-{path.name}-{os.getpid()}")
        try:
            os.replace(path, taken)
        except OSError:
            return []
        try:
            lines = taken.read_text(encoding="utf-8", errors="replace").splitlines()
        finally:
            taken.unlink(missing_ok=True)
        runs = []
        for line in lines:
            try:
                run = json.loads(line)
            except ValueError:
                continue
            runs += [run] if isinstance(run, dict) else []
        return runs

    def sweep(self, now: float) -> None:
        """Delete snapshots, private indexes and runs files older than a day."""
        if self.data_dir is None:
            return
        try:
            entries = list(self.data_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            name = entry.name.lstrip(".")
            if name.startswith((*_TEMP_PREFIXES, "taken-", "tmp-")):
                try:
                    if now - entry.stat().st_mtime > STALE_AFTER_S:
                        entry.unlink()
                except OSError:
                    continue


def effective_prompt_only(config: Config, store: Store) -> bool:
    """Prompt-only unless both the person and the server said otherwise.

    Unknown counts as prompt-only: before the server has answered once, the
    client can't know the person turned it on at the website.
    """
    return config.prompt_only or store.server_prompt_only() is not False


def _valid_ids(*ids: str | None) -> bool:
    return all(isinstance(i, str) and _ID.fullmatch(i) for i in ids)


def _capabilities(profile: AgentProfile) -> dict[str, bool]:
    caps = profile.capabilities
    return {
        "can_block_prompt": caps.can_block_prompt,
        "has_reply": caps.has_reply,
        "has_exit_codes": caps.has_exit_codes,
    }


def before(event: PromptSubmitted, config: Config, profile: AgentProfile) -> Reply | None:
    """Coach a prompt before the agent sees it.

    The start-of-turn snapshot is taken alongside the request (unless the
    person chose prompt-only mode), so the two together fit the hook's time
    limit. A prompt the server blocks never reaches the agent, so its
    snapshot is dropped.
    """
    store = Store(config.data_dir)
    store.sweep(time.time())
    if not _valid_ids(event.session_id, event.prompt_id):
        return None
    if not config.token:
        if store.first_warning(event.session_id):
            return Reply(profile.missing_token_message, False, True)
        return None
    trees: list[str] = []
    index = store.index_path(event.session_id)
    snapper = None
    if not config.prompt_only and index is not None:

        def take() -> None:
            tree = gitdiff.snapshot(event.cwd, index, BEFORE_SNAPSHOT_S)
            trees.extend([tree] if tree else [])

        snapper = threading.Thread(target=take, daemon=True)
        snapper.start()
    prompt_only = effective_prompt_only(config, store)
    body = {
        "agent": profile.agent,
        "plugin_version": profile.plugin_version,
        "session_id": event.session_id,
        "prompt_id": event.prompt_id,
        "prompt": event.prompt[:MAX_PROMPT_CHARS],
        "setup": collect_setup(
            event.project_dir, config.home, profile.sources, prompt_only=prompt_only
        ),
        "capabilities": _capabilities(profile),
    }
    started = time.monotonic()
    reply = parse_reply(
        post(endpoint(config.server_url, "/v1/before"), config.token, body, BEFORE_HTTP_TIMEOUT_S)
    )
    if snapper is not None:
        snapper.join(max(BEFORE_SNAPSHOT_S - (time.monotonic() - started), 0.0))
    if reply is not None:
        store.set_server_prompt_only(reply.prompt_only)
    if trees and not (reply is not None and reply.block):
        store.save_snapshot(event.session_id, event.prompt_id, trees[0])
    return reply


def record(event: ToolRan, config: Config) -> None:
    """Note one shell command the agent ran, for the end of the turn. No network.

    If the person chose prompt-only mode, the output is never even written.
    """
    if not _valid_ids(event.session_id):
        return
    run: dict[str, object] = {
        "prompt_id": event.prompt_id,
        "command": event.command[:MAX_COMMAND_CHARS],
        "exit_code": event.exit_code,
        "output_tail": "" if config.prompt_only else event.output[-MAX_OUTPUT_CHARS:],
        "is_test": bool(IS_TEST.search(event.command)),
    }
    Store(config.data_dir).append_run(event.session_id, run)


def _tool_runs(
    runs: list[dict[str, Any]], prompt_id: str, prompt_only: bool
) -> list[dict[str, Any]]:
    """This turn's runs, as the server's ``ToolRun``; the last 50 of them."""
    out = []
    for run in runs:
        command, code, output = run.get("command"), run.get("exit_code"), run.get("output_tail")
        if run.get("prompt_id") != prompt_id or not isinstance(command, str):
            continue
        out.append(
            {
                "command": command_skeleton(command)
                if prompt_only
                else command[:MAX_COMMAND_CHARS],
                "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
                "output_tail": ""
                if prompt_only or not isinstance(output, str)
                else output[-MAX_OUTPUT_CHARS:],
                "is_test": bool(IS_TEST.search(command)),
            }
        )
    return out[-MAX_TOOL_RUNS:]


def after(event: TurnEnded, config: Config, profile: AgentProfile) -> Reply | None:
    """Coach a finished turn: this turn's diff, its tool runs, the agent's reply.

    A turn with no start-of-turn snapshot (the agent ran a shell command the
    person typed, or the plugin was installed mid-session) is normal: it
    falls back to ``git diff HEAD``. The snapshot and the runs file are
    removed whatever happens.
    """
    if event.continuing:
        return None
    store = Store(config.data_dir)
    if not _valid_ids(event.session_id, event.prompt_id):
        return None
    start_tree = store.take_snapshot(event.session_id, event.prompt_id)
    runs = store.take_runs(event.session_id)
    if not config.token:
        return None
    prompt_only = effective_prompt_only(config, store)
    diff = None
    if not prompt_only:
        index = store.index_path(event.session_id)
        end_tree = gitdiff.snapshot(event.cwd, index) if start_tree and index else None
        if start_tree and end_tree:
            diff = gitdiff.diff_trees(event.cwd, start_tree, end_tree)
        else:
            diff = gitdiff.collect_diff(event.cwd)
    reply_text = None
    if profile.capabilities.has_reply and event.reply is not None and not prompt_only:
        reply_text = event.reply[-MAX_REPLY_CHARS:]
    body = {
        "agent": profile.agent,
        "plugin_version": profile.plugin_version,
        "session_id": event.session_id,
        "prompt_id": event.prompt_id,
        "diff": diff,
        "tool_runs": _tool_runs(runs, event.prompt_id, prompt_only),
        "last_assistant_message": reply_text,
        "prompt_only": prompt_only,
        "capabilities": _capabilities(profile),
    }
    reply = parse_reply(
        post(endpoint(config.server_url, "/v1/after"), config.token, body, AFTER_HTTP_TIMEOUT_S)
    )
    if reply is None:
        return None
    store.set_server_prompt_only(reply.prompt_only)
    return Reply(reply.message, False, reply.prompt_only)
