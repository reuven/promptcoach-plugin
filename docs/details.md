# Prompt coach: the details

The [README](../README.md) gives the short version. This page is for anyone who
wants to check what the plugin sends, filters and runs.

## What is sent

Everything goes over HTTPS to `coach.lernerpython.com`.

**With each prompt:**

- the prompt you typed;
- your instructions files, up to 20,000 characters each:
  - project: `CLAUDE.md`, `AGENTS.md`, `SYSTEM.md`, `.claude/CLAUDE.md`;
  - global: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.pi/agent/AGENTS.md`;
- facts about your setup, with no code. Examples: whether `.gitignore` covers
  `.env`, the test command, whether there's CI, which linters are configured,
  and which known tools your hooks run (never the hook commands themselves).

**When Claude finishes a turn:**

- the diff of that turn only. A snapshot is taken before the turn, so earlier
  changes and your own edits aren't included;
- the shell commands Claude ran, with their exit codes and the last 4,000
  characters of their output;
- Claude's final reply.

## Secrets

**Credential files are never sent.** `.env` files, private keys, `*.pem`,
`credentials.json`, `.netrc`, cloud credentials and the like are left out of
every snapshot and diff on your machine. The server refuses them too.

**Secrets elsewhere are filtered** before anything leaves your machine:

- A command that reads a credential file (`cat .env`), prints the environment
  (`env`, `printenv`) or asks a tool for a secret (`gh auth token`) has its
  output dropped.
- Well-known token shapes (`sk-...`, `ghp_...`, `AKIA...`, JWTs,
  `Authorization:` headers, passwords in URLs or on command lines) become
  `[REDACTED]` in commands, output, replies and diffs.
- So do values on lines like `DB_PASSWORD=...` or `api_key: ...`. Plain code
  such as `password = get_password()` is left alone.
- The server scrubs secrets again from everything it stores.

This is a filter for the usual cases, not a guarantee. A secret in an unusual
shape, or a command that hides what it reads (`eval`, a file name held in a
variable), can get through. Keep secrets out of code and output, or use
prompt-only mode.

## How the diff is made

The plugin snapshots your work tree with its own private git index file.

- Your index, staging area and stash are never touched.
- A snapshot writes unreferenced objects into `.git/objects`; `git gc`
  removes them in time.
- The plugin never runs programs named by the repository's git config
  (hooks, fsmonitor, diff drivers) and never fetches.
- A submodule shows up only as a commit line, never its files.
- Only your project's directory is covered. In a monorepo, the rest of the
  repository is left out.

Some repositories get no diff at all:

- one whose config defines a clean/smudge filter;
- one whose `.git` points somewhere else or contains a symlink (a linked
  worktree its main repository lists is fine);
- one whose `.git` holds a special file or has more than 100,000 entries;
- one whose top is your home directory or above it, such as a dotfiles
  repository.

These checks protect you from a repository someone handed you. They can't
protect you from a program already running as you, which could read your
files directly anyway.

Files are read only where they are. An instructions file or `.claude/` that is
a symlink out of the project, or to a credential file, counts as absent.

## Prompt-only mode

For code you may not send anywhere (your employer's, say), turn on
prompt-only mode in either place:

- in the plugin: `/plugin` → promptcoach → Configure → Prompt-only mode;
- on your settings page: <https://coach.lernerpython.com/settings>.

In prompt-only mode:

- no diffs, command output or Claude replies are sent;
- commands are cut to an outline: the program, its subcommands, option names
  without values, and file paths. `uv run pytest tests/x.py -k slow` is sent
  as `uv run pytest tests/x.py -k`;
- your prompts, instructions files and setup facts are still sent.

Until the plugin has heard from the server once, it acts as if prompt-only
mode were on.

## Privacy

- Only you and I (Reuven) can see your history.
- To write feedback, the coach sends your prompts to Anthropic's API, and
  your code too unless prompt-only mode is on.
- You can opt out of cross-subscriber learning, or delete everything, at
  <https://coach.lernerpython.com/settings>.
- The full notice is shown when you sign in at
  <https://coach.lernerpython.com/signin>.

## Time limits

Each hook has a fixed time budget for all its work:

- 2.5 seconds before a prompt;
- 13.5 seconds at the end of a turn.

Claude Code's own limits (5 and 20 seconds) leave room for the shell and
Python to start. A slow git or a slow server means less coaching that turn,
never a stuck session.

**Ordinary prompts fail open.** If the coach doesn't answer in time, the
prompt goes to Claude with no coaching that turn. The coach can never keep an
ordinary prompt from Claude.

**Coach commands fail closed.** A command such as `coach, quieter` never
reaches Claude, whatever happens:

- if the coach doesn't answer in time, you see "Coach: I didn't answer in
  time, so that command wasn't applied. Please send it again.";
- if the coach answers with a notice (sign in, renew your membership), you
  see that notice;
- before your token is set, you see a reminder to add it.

If Claude Code stops a hook at its limit, any git the hook started stops with
it. A hook killed outright (`kill -9`) can't clean up, so its git runs to the
end. That takes about as long as `git add -A` on your project, and writes
only the plugin's private index and unreferenced objects.

## Versions

- The plugin and the coach server share one version and are released
  together. Every request names the plugin's version.
- Versions follow a strict subset of PEP 440, such as `1.0.0` or `1.0.0b1`.
- **The server is deployed first, then the plugin.** Until the server
  accepts a new plugin version, that plugin gets no coaching.
