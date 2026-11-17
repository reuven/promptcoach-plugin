"""Secrets the client keeps to itself: a first filter before anything is sent.

The server scrubs everything again; this is not a port of its scrubber. It
does two cheap things, both failing closed:

- ``prints_secrets``: a command that reads a credential file (``cat .env``,
  ``source .env``, ``< .env``, or a glob such as ``.env*`` that could match
  one), expands a secret-named variable (``$OPENAI_API_KEY``), dumps the
  environment (``env``, ``printenv``, ``set``, ``export -p``,
  ``os.environ``) or asks a tool for a secret (``gh auth token``,
  ``kubectl get secret``, ``docker compose config``, ``dotenv_values``) has
  its output dropped, so it is never even written to the plugin's data
  directory. Contrived shell tricks (``eval``, ``$'\x2e'env``, a variable
  holding the file name) are out of scope: the server scrubs again.
- ``redact``: well-known token shapes (``sk-``, ``ghp_``, ``AKIA``,
  ``sk_live_``, ``hf_``, ``npm_``, ``ya29.``, JWTs...), ``Authorization``
  headers, passwords in URLs, private-key blocks (also one whose start was
  cut off), and secret-named values (see ``redact``) become ``[REDACTED]``
  in commands, command output, the agent's reply and the diff.

Every pattern is linear: bounded repeats only, no nested quantifiers, so a
megabyte of hostile text takes milliseconds.
"""

from __future__ import annotations

import fnmatch
import re

from .forbidden import (
    FORBIDDEN_NAMES,
    FORBIDDEN_PATH_ENDINGS,
    FORBIDDEN_SUFFIXES,
    SSH_KEY_NAMES,
    is_forbidden_path,
)

REDACTED = "[REDACTED]"

_TOKENS = re.compile(
    "|".join(
        [
            r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}",
            r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}",
            r"\bgithub_pat_[A-Za-z0-9_]{20,}",
            r"\bxox[abp]-[A-Za-z0-9-]{10,}",
            r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
            r"\bAIza[A-Za-z0-9_-]{30,}",
            r"\bglpat-[A-Za-z0-9_-]{20,}",
            r"\bBearer [A-Za-z0-9._~+/=-]{16,}",
            r"\b[srp]k_(?:live|test)_[A-Za-z0-9]{10,}",
            r"\bhf_[A-Za-z0-9]{20,}",
            r"\bnpm_[A-Za-z0-9]{30,}",
            r"\bya29\.[A-Za-z0-9_-]{10,}",
            r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*",
        ]
    )
)
# Kept: the header name and scheme. Gone: the credential after it.
_AUTH = re.compile(
    r"(\bAuthorization[\"']?[ \t]{0,8}[:=][ \t]{0,8}[\"']?(?:Basic|Bearer|Token)[ \t]{1,8})"
    r"[^\s\"']+",
    re.IGNORECASE,
)
# scheme://user:password@host
_USERINFO = re.compile(r"(://[^/\s:@]{1,200}:)[^/\s@]{1,500}(@)")
# A private key, to its END line or, if cut off, to the end of the text.
_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"
    r"(?:[^-]|-(?!----END ))*"
    r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----)?"
)
# The end of a key whose start was cut off (a tail of command output).
_KEY_END = re.compile(r"\A[A-Za-z0-9+/=\s]*-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")
# The NAME of NAME=value or NAME: value, starting a word. Whether the name
# says secret is checked on the (short) match, which keeps this linear.
_NAME = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]{1,80})[\"']?[ \t]{0,8}([=:])")
_SECRET_NAME = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|(?<![A-Za-z])PASS(?![A-Za-z])",
    re.IGNORECASE,
)
_ENV_STYLE = re.compile(r"[A-Z0-9_.-]*[A-Z][A-Z0-9_.-]*")
# Code, not a value: an identifier, attribute or call (``api_key``,
# ``settings.api_key``, ``draw()``, ``AutoTokenizer.from_pretrained(...)``).
_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*(?:\(.*)?[,;:)\]]*")
_NUMBER = re.compile(r"[-+]?[0-9][0-9_.]*[,;]?")
_LITERAL_MIN = 8
_VALUE_LOOK = 256  # how much of a value is looked at: keeps a long line linear

# Programs that print the environment: printenv always; env with no program
# to run ("env", "env -i FOO=1"); export, declare and typeset with flags only
# ("export -p", "declare -x"); set with nothing after it.
_FLAGS_ONLY = frozenset({"export", "declare", "typeset"})
_WRAPPERS = frozenset({"sudo", "command", "builtin", "exec", "time", "nohup"})
_WORD = re.compile(r"[^\s;|&()<>`]+")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SEGMENT = re.compile(r"[;|&\n()`]|\$\(")
_VARIABLE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_SECRET_VARIABLE = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|_(?:KEY|PAT|PASS)\Z",
    re.IGNORECASE,
)
_MARKERS = re.compile(
    r"os\.environ|dotenv_values|compose\s+config|auth\s+token|get\s+secret",
    re.IGNORECASE,
)
_GLOB = re.compile(r"[*?\[]")
# Names a glob is tried against: one of each kind of credential file.
_SAMPLES = (
    *FORBIDDEN_NAMES,
    *(f"x{suffix}" for suffix in FORBIDDEN_SUFFIXES),
    *SSH_KEY_NAMES,
    ".env.local",
)
_SAMPLE_PATHS = tuple(ending.strip("/").split("/") for ending in FORBIDDEN_PATH_ENDINGS)


def _glob_may_match(word: str) -> bool:
    """True if a shell glob could expand to a credential file's name."""
    parts = word.rstrip("/").split("/")
    if any(fnmatch.fnmatchcase(sample, parts[-1]) for sample in _SAMPLES):
        return True
    return any(
        len(parts) >= len(path) and all(map(fnmatch.fnmatchcase, path, parts[-len(path) :]))
        for path in _SAMPLE_PATHS
    )


def prints_secrets(command: str) -> bool:
    """True if ``command`` plainly prints credentials: its output isn't kept.

    It names a credential file anywhere (after ``cat``, ``source``, ``.``,
    ``<`` or anything else, quoted or not, or as a glob that could match
    one), expands a secret-named variable, runs a program that dumps the
    environment, or asks a tool for a secret. The whole command is read,
    however long.
    """
    if _MARKERS.search(command):
        return True
    if any(_SECRET_VARIABLE.search(name) for name in _VARIABLE.findall(command)):
        return True
    for raw in _WORD.findall(command):
        word = raw.replace("\\", "").replace("'", "").replace('"', "")
        if is_forbidden_path(word) or (_GLOB.search(word) and _glob_may_match(word)):
            return True
    for segment in _SEGMENT.split(command):
        words = _WORD.findall(segment)
        while words and (words[0] in _WRAPPERS or _ASSIGNMENT.match(words[0])):
            words = words[1:]
        program, args = (words[0].rsplit("/", 1)[-1], words[1:]) if words else ("", [])
        flags_only = all(w.startswith("-") for w in args)
        if (
            program == "printenv"
            or (program == "env" and all(w.startswith("-") or "=" in w for w in args))
            or (program in _FLAGS_ONLY and flags_only)
            or (program == "set" and not args)
        ):
            return True
    return False


def _is_literal(name: str, separator: str, value: str) -> bool:
    """True if ``value`` (the rest of the line after ``NAME=``) is a secret's value.

    Why so careful: diffs are code, and ``max_tokens=1024``,
    ``self.api_key = api_key`` or ``tokenizer = AutoTokenizer...`` must reach
    the coach intact. A value counts when it is quoted, or an unquoted run
    of 8+ characters that isn't an identifier, attribute or call; an
    UPPER_CASE (environment-style) name counts with any value, except a
    bare number or type for a count of tokens (``MAX_TOKENS = 4096``).
    """
    if value.startswith(("=", "/")) or (separator == ":" and value.startswith("=")):
        return False  # ==, //, a comment, := ...
    first = value.split()[0] if value.split() else ""
    if name.lower().endswith("tokens") and (
        _NUMBER.fullmatch(first) or (separator == ":" and _CODE.fullmatch(first))
    ):
        return False
    if _ENV_STYLE.fullmatch(name):
        return True
    if value.startswith(("'", '"')):
        return True
    if _CODE.fullmatch(first) and ("_" in first or "." in first or "(" in first or first.isalpha()):
        return False
    return len(first) >= _LITERAL_MIN


def _next_line_value(text: str, line_start: int, end: int) -> tuple[int, int] | None:
    """For YAML ``password:`` with nothing after it: the indented value on
    the next line, as (start, end) of the text to replace; else None."""
    if end >= len(text):
        return None
    indent = len(text[line_start:]) - len(text[line_start:].lstrip(" \t"))
    start = end + 1
    stop = text.find("\n", start)
    stop = len(text) if stop < 0 else stop
    line = text[start:stop]
    stripped = line.lstrip(" \t")
    if not stripped or len(line) - len(stripped) <= indent or _NAME.match(stripped):
        return None
    return start + len(line) - len(stripped), stop


def redact(text: str) -> str:
    """``text`` with token shapes, private keys and secret values replaced.

    A ``NAME=value`` or ``NAME: value`` loses its value (to the end of the
    line) when the name says password, pass, secret, token or key and the
    value is a literal (see ``_is_literal``); YAML's ``password:`` followed
    by an indented value on the next line loses that value.
    """
    text = _KEY_BLOCK.sub(REDACTED, text)
    text = _KEY_END.sub(REDACTED, text)
    text = _TOKENS.sub(REDACTED, text)
    text = _AUTH.sub(lambda m: m[1] + REDACTED, text)
    text = _USERINFO.sub(lambda m: m[1] + REDACTED + m[2], text)
    out: list[str] = []
    done = 0  # text before this is already in ``out``
    for match in _NAME.finditer(text):
        if match.start() < done or not _SECRET_NAME.search(match[1]):
            continue
        end = text.find("\n", match.end())
        end = len(text) if end < 0 else end
        value = text[match.end() : min(end, match.end() + _VALUE_LOOK)].strip()
        if value:
            if _is_literal(match[1], match[2], value):
                out += [text[done : match.end()], REDACTED]
                done = end
        elif match[2] == ":":
            line_start = text.rfind("\n", 0, match.start()) + 1
            span = _next_line_value(text, line_start, end)
            if span is not None:
                out += [text[done : span[0]], REDACTED]
                done = span[1]
    out.append(text[done:])
    return "".join(out)
