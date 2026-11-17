# Prompt coach for Claude Code

Prompt coach is a Claude Code plugin for LernerPython subscribers. Before Claude
sees each prompt, and again when Claude finishes its turn, it asks the coach
server for short, specific feedback on how you prompted and on how your project
is set up for agentic coding (instructions files, tests, hooks, CI). The
feedback is shown to you, never to Claude: it doesn't use up Claude's context,
and Claude never acts on it by itself.

## Install

1. Get your coach token: sign in at <https://coach.lernerpython.com/signin>.
   The token is shown once, so keep the page open until you've pasted it.
2. In Claude Code, add this marketplace and install the plugin:

   ```
   /plugin marketplace add reuven/promptcoach-plugin
   /plugin install promptcoach@promptcoach
   ```

3. Paste your token when Claude Code asks for it. Claude Code keeps it in
   your system's credential store, not in a file.
4. Turn on auto-update: `/plugin` → Marketplaces → promptcoach → enable
   auto-update. Third-party marketplaces don't update on their own.

The hooks need `python3` 3.10 or newer on your `PATH`; if yours is older,
the coach says so once per session and otherwise stays out of the way.
Nothing else is installed: the hook code uses only Python's standard library.

To change your token or settings later: `/plugin` → promptcoach → Configure.

## What is sent

Each prompt sends, over HTTPS, to `coach.lernerpython.com`:

- the prompt you typed;
- your project's instructions files (`CLAUDE.md`, `AGENTS.md`, `SYSTEM.md`,
  `.claude/CLAUDE.md`) and your global ones (`~/.claude/CLAUDE.md`,
  `~/.codex/AGENTS.md`, `~/.pi/agent/AGENTS.md`), up to 20,000 characters each;
- facts about the setup, with no code: whether `.gitignore` covers `.env` and
  data files, the test command, whether there's a tests directory, CI or
  pre-commit, which linters are configured, which hook events you use and
  which known tools (such as `ruff` or `pytest`) those hooks run (never the
  hook commands themselves), skill names, and which data libraries the
  project depends on (names only).

When Claude finishes a turn, it sends:

- the diff of what changed during that turn only (a snapshot is taken before
  the turn, so earlier changes and your own edits aren't included);
- the shell commands Claude ran, their exit codes and the last 4,000
  characters of their output;
- Claude's final reply.

Never sent: `.env` files, `.envrc`, private keys, `*.pem`, `credentials.json`,
`.netrc`, `.pypirc`, `.npmrc`, cloud credentials and similar files. They are
left out of every snapshot and diff on your machine, and the server refuses
them too. The server also scrubs secrets from everything it stores.

The snapshots are made with a private git index file, so your own index,
staging area and stash are never touched.

## Prompt-only mode

For code you may not send anywhere (your employer's, say), turn on
prompt-only mode, either in the plugin (`/plugin` → promptcoach → Configure →
Prompt-only mode) or on your settings page at
<https://coach.lernerpython.com/settings>. Either one is enough. In
prompt-only mode:

- no diffs, no command output and no Claude reply are sent;
- commands are reduced to their program and plain options (`python -c '...'`
  is sent as `python -c`);
- your prompts, your instructions files and the setup facts above are still
  sent.

Until the plugin has heard from the server once, it behaves as if
prompt-only mode were on.

## Privacy

Only you can see your own history. To write feedback, the coach sends your
prompts, and your code unless prompt-only mode is on, to Anthropic's API. You
can opt out of cross-subscriber learning, or delete everything, at any time
from <https://coach.lernerpython.com/settings>. The full notice is shown when
you sign in at <https://coach.lernerpython.com/signin>.

## Development

```
uv sync
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest
claude plugin validate ./plugins/promptcoach --strict
claude plugin validate .
```

The hook code lives in `plugins/promptcoach/hooks/`: `coach_hook.py` is the
entry point, `promptcoach_client/core.py` and its helpers are agent-neutral,
and `promptcoach_client/claude_code.py` is the Claude Code adapter.
