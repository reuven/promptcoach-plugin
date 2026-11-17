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

The hooks need `python3` 3.9 or newer on your `PATH` (the one macOS ships
is fine); if yours is older, the coach says so once per session and
otherwise stays out of the way. Nothing else is installed: the hook code uses
only Python's standard library, and runs as `python3 -I`, so modules in your
project (or on your `PYTHONPATH`) are never loaded into it.

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

Credential files (`.env` files, `.envrc`, private keys, `*.pem`,
`credentials.json`, `.netrc`, `.pypirc`, `.npmrc`, cloud credentials and
similar files) are left out of every snapshot and diff on your machine, and
the server refuses them too.

Secrets can still turn up elsewhere: in a command's output, in Claude's
reply, or pasted into a source file. The plugin filters the common ones
before anything leaves your machine. A command that reads a credential file
(`cat .env`, `source .env`, `< .env`) or prints the environment (`env`,
`printenv`, `export -p`) has its output dropped, and well-known token shapes
(`sk-...`, `ghp_...`, `AKIA...`, private-key blocks and the like) and the
values of lines such as `DB_PASSWORD=...` or `api_key: ...` are replaced with
`[REDACTED]` in output, replies and diffs. The server scrubs secrets again
from everything it stores. This is a filter for the usual cases, not a
guarantee: a secret in an unusual shape can get through, so keep secrets out
of code and output, or use prompt-only mode.

The snapshots are made with a private git index file, so your own index,
staging area and stash are never touched. Making one does write git objects
(the blobs and trees of your work tree) into the repository's
`.git/objects`; nothing refers to them, and `git gc` removes them in time.
The plugin never runs programs a repository's own git config names (an
fsmonitor hook, hooks, diff drivers), never fetches (a partial clone's
missing objects stay missing), and shows a submodule only as a commit line,
never its files. A repository whose own config defines a clean/smudge filter
gets no diff at all, and so does one whose `.git` points somewhere else (a
`gitdir:` file, `core.worktree`, a `commondir` file, `alternates`, or a
`.git` that is, or contains, a symlink anywhere), unless it is a linked
worktree that its main repository lists. A `.git` directory too big to check
quickly (over 100,000 entries) gets no diff either. Files are read only where they are: a `CLAUDE.md` or `.claude/` that is
a symlink out of the project (or out of your home directory, for the global
files), or to a credential file, is treated as absent.

## Prompt-only mode

For code you may not send anywhere (your employer's, say), turn on
prompt-only mode, either in the plugin (`/plugin` → promptcoach → Configure →
Prompt-only mode) or on your settings page at
<https://coach.lernerpython.com/settings>. Either one is enough. In
prompt-only mode:

- no diffs, no command output and no Claude reply are sent;
- commands are reduced to an outline: the program, its known subcommands,
  option names without their values, and file paths (a word with a `/`, or
  ending in a common extension such as `.py`, `.toml` or `.csv`)
  (`uv run pytest tests/x.py -k slow` is sent as `uv run pytest tests/x.py -k`,
  `python -c '...'` as `python -c`, `mysql -p secret` as `mysql`);
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

## Time limits

Each hook has one time budget for all its work: 2.6 seconds before a prompt
and 13.5 seconds at the end of a turn, inside Claude Code's own limits. A
slow git or a slow server means less coaching for that turn, never a stuck
session. The server can keep a prompt from Claude only when the prompt is a
coach command such as `coach, quieter`.

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
uv run --isolated --no-project --python 3.9 --with 'pytest<9' --with pytest-cov pytest
claude plugin validate ./plugins/promptcoach --strict
claude plugin validate .
```

The hook code lives in `plugins/promptcoach/hooks/`: `coach_hook.py` is the
entry point, `promptcoach_client/core.py` and its helpers are agent-neutral,
and `promptcoach_client/claude_code.py` is the Claude Code adapter.
