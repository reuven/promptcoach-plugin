"""Secrets the client keeps to itself: a first filter before anything is sent.

The server scrubs everything again; this is not a port of its scrubber. It
does two cheap things, both failing closed:

- ``prints_secrets``: a command that reads a credential file (``cat .env``,
  ``source .env``, ``< .env``) or dumps the environment (``env``,
  ``printenv``, ``set``, ``export -p``, ``declare -x``) has its output
  dropped, so it is never even written to the plugin's data directory.
- ``redact``: well-known token shapes (``sk-``, ``ghp_``, ``AKIA``...),
  private-key blocks, and the value of any ``NAME=value`` or ``NAME: value``
  whose name says password, secret, token or key become ``[REDACTED]`` in
  command output, the agent's reply and the diff.

Every pattern is linear: bounded repeats only, no nested quantifiers, so a
megabyte of hostile text takes milliseconds.
"""

from __future__ import annotations

import re

from .forbidden import is_forbidden_path

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
        ]
    )
)
# A private key, to its END line or, if cut off, to the end of the text.
_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"
    r"(?:[^-]|-(?!----END ))*"
    r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----)?"
)
# The NAME of NAME=value or NAME: value, starting a word. Whether the name
# says secret is checked on the (short) match, which keeps this linear.
_NAME = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]{1,80})[\"']?[ \t]{0,8}[=:]")
_SECRET_NAME = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY", re.IGNORECASE)

# Programs that print the environment: printenv always; env with no program
# to run ("env", "env -i FOO=1"); export, declare and typeset with flags only
# ("export -p", "declare -x"); set with nothing after it.
_FLAGS_ONLY = frozenset({"export", "declare", "typeset"})
_WRAPPERS = frozenset({"sudo", "command", "builtin", "exec", "time", "nohup"})
_WORD = re.compile(r"[^\s;|&()<>'\"`]+")
_SEGMENT = re.compile(r"[;|&\n()`]|\$\(")
_MAX_SCAN = 10_000


def prints_secrets(command: str) -> bool:
    """True if ``command`` plainly prints credentials: its output isn't kept.

    It names a credential file anywhere (after ``cat``, ``source``, ``.``,
    ``<`` or anything else), or runs a program that dumps the environment.
    """
    command = command[:_MAX_SCAN]
    if any(is_forbidden_path(word) for word in _WORD.findall(command)):
        return True
    for segment in _SEGMENT.split(command):
        words = _WORD.findall(segment)
        while words and words[0] in _WRAPPERS:
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


def redact(text: str) -> str:
    """``text`` with token shapes, private keys and secret-named values replaced."""
    text = _KEY_BLOCK.sub(REDACTED, text)
    text = _TOKENS.sub(REDACTED, text)
    out: list[str] = []
    done = 0  # text before this is already in ``out``
    for match in _NAME.finditer(text):
        if match.start() < done or not _SECRET_NAME.search(match[1]):
            continue
        end = text.find("\n", match.end())
        end = len(text) if end < 0 else end
        if end > match.end():
            out += [text[done : match.end()], REDACTED]
            done = end
    out.append(text[done:])
    return "".join(out)
