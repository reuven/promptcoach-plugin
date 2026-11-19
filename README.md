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

- **Credential files are never sent:** `.env`, private keys, cloud
  credentials and the like.
- **Tokens and passwords become `[REDACTED]`** before anything leaves your
  machine.
- **Commands that print secrets** (`cat .env`, `printenv`) have their output
  dropped.
- **The server scrubs secrets again** before storing anything.

This catches the usual cases, not every case. Keep secrets out of your code
and output, or use prompt-only mode.

The plugin never changes your git index, staging area or stash, and never
runs a repository's own hooks.

For the full rules, see [docs/details.md](docs/details.md).

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

- **An ordinary prompt** goes to Claude, with no coaching that turn.
- **A `coach, …` command** is still kept from Claude. The coach tells you it
  wasn't applied, so send it again.

## Versions

The plugin and the coach server share one version number and are released
together.

## License

MIT; see [LICENSE](LICENSE).

## Development

Where the code lives, in `plugins/promptcoach/hooks/`:

- `coach_hook.py`: the entry point;
- `promptcoach_client/core.py` and its helpers: code shared by every agent;
- `promptcoach_client/claude_code.py`: the Claude Code adapter.

To run the checks:

```
uv sync

# Format, lint and types
uv run ruff format --check
uv run ruff check
uv run mypy
uv run mypy --python-version 3.10 tests

# Tests, on your Python and on the oldest supported one (3.9)
uv run pytest
uv run --isolated --no-project --python 3.9 \
  --with 'pytest<9' --with pytest-cov --with hypothesis pytest

# The plugin and marketplace manifests
claude plugin validate ./plugins/promptcoach --strict
claude plugin validate .
```
