# Prompt coach for Claude Code

Prompt coach is a Claude Code plugin for LernerPython members. It gives you
short, specific feedback on your prompts, and on how your project is set up
for agentic coding: instructions files, tests, hooks and CI.

- It checks each prompt before Claude sees it, and again when Claude finishes.
- The feedback is shown to you, never to Claude, so it doesn't use up
  Claude's context.

## Install

1. Get your coach token: sign in at <https://coach.lernerpython.com/signin>.
   It's shown once, so keep the page open until you've pasted it.
2. In Claude Code, add this marketplace and install the plugin:

   ```
   /plugin marketplace add reuven/promptcoach-plugin
   /plugin install promptcoach@promptcoach
   ```

3. Paste your token when Claude Code asks for it. Claude Code keeps it in
   your system's credential store, not in a file.
4. Turn on auto-update: `/plugin` → Marketplaces → promptcoach → enable
   auto-update. Third-party marketplaces don't update on their own.

You need Python 3.9 or newer:

- **macOS and Linux:** `python3`. The one macOS ships is fine.
- **Windows:** the `py` launcher, or Python from python.org or uv. The
  Microsoft Store alias isn't used. See [docs/windows.md](docs/windows.md).

Nothing else is installed: the hooks use only Python's standard library.

To change your token or settings later: `/plugin` → promptcoach → Configure.

## Talking to the coach

Start a prompt with `coach,` to change a setting. These never reach Claude:

- `coach, quieter` or `coach, louder`: less or more feedback
- `coach, off` or `coach, on`
- `coach, status`
- `coach, prompt-only on` or `coach, prompt-only off`
- `coach, helpful` or `coach, not helpful`: rate the last note

## What is sent

Everything goes over HTTPS to `coach.lernerpython.com`.

**With each prompt:**

- the prompt you typed;
- your instructions files, such as `CLAUDE.md` and `AGENTS.md`;
- facts about your setup, with no code: for example, whether you have
  tests, CI or pre-commit, and which linters you use.

**When Claude finishes a turn:**

- the diff of what changed during that turn only;
- the shell commands Claude ran, with their exit codes and the end of their
  output;
- Claude's final reply.

## Secrets

- **Credential files are never sent.** That includes `.env`, private keys,
  `credentials.json`, `.netrc` and cloud credentials.
- **Common secrets are removed before anything leaves your machine.**
  - API keys and tokens, such as `sk-...`, `ghp_...` and `AKIA...`, are
    replaced with `[REDACTED]`.
  - So are passwords, in URLs and in lines like `DB_PASSWORD=...`.
  - Output is dropped entirely from commands that print secrets, such as
    `cat .env`, `printenv` and `gh auth token`.
- **The server scrubs secrets again** before storing anything.

This catches the usual cases, but it can't catch everything. So keep secrets
out of your code and output, or use prompt-only mode.

The plugin is careful with git too. It never changes your index, staging
area or stash, and it never runs a repository's own hooks or filters.

[docs/details.md](docs/details.md) has more detail, for anyone who wants to
check what's sent, filtered and run.

## Prompt-only mode

For code you may not send anywhere (your employer's, say), turn on
prompt-only mode. Either of these is enough:

- in the plugin: `/plugin` → promptcoach → Configure → Prompt-only mode;
- on your settings page at <https://coach.lernerpython.com/settings>.

In prompt-only mode:

- no diffs, command output or Claude replies are sent;
- commands are reduced to an outline: `uv run pytest tests/x.py -k slow`
  becomes `uv run pytest tests/x.py -k`;
- your prompts, instructions files and setup facts are still sent.

## Privacy

- **Who sees your history:** only you and I (Reuven).
- **Where it goes:** to write feedback, the coach sends your prompts, and
  your code unless prompt-only mode is on, to Anthropic's API.
- **What you control:** you can opt out of shared learning, or delete
  everything, at <https://coach.lernerpython.com/settings>.

The full notice is shown when you sign in.

## If the coach is slow

The coach never holds up your session.

- **An ordinary prompt:** if the coach doesn't answer in time, the prompt
  goes to Claude, with no coaching that turn.
- **A `coach, …` command:** if the coach doesn't answer in time, the command
  is *not* passed to Claude. You'll see "Coach: I didn't answer in time, so
  that command wasn't applied. Please send it again."

## Versions

The plugin and the coach server always share one version number, and are
released together.

## License

MIT; see [LICENSE](LICENSE).

## Development

```
uv sync
uv run ruff format --check
uv run ruff check
uv run mypy
uv run mypy --python-version 3.10 tests
uv run pytest
uv run --isolated --no-project --python 3.9 --with 'pytest<9' --with pytest-cov --with hypothesis pytest
claude plugin validate ./plugins/promptcoach --strict
claude plugin validate .
```

The hook code lives in `plugins/promptcoach/hooks/`: `coach_hook.py` is the
entry point, `promptcoach_client/core.py` and its helpers are agent-neutral,
and `promptcoach_client/claude_code.py` is the Claude Code adapter.
