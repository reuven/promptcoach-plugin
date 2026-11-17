"""Credential files the client never sends, mirrored from the server.

Why: the server already drops these from every diff it receives, but the
promise is that they never leave the person's machine. The lists copy
``is_forbidden_path`` in the server's ``diffparse.py`` (a test pins them), and
``exclude_pathspecs`` turns them into git ``:(exclude)`` pathspecs so the
snapshot and the diff skip them before any byte is read.
"""

from __future__ import annotations

import re

FORBIDDEN_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        "credentials.json",
        ".netrc",
        ".pypirc",
        ".npmrc",
        ".pgpass",
        ".htpasswd",
        ".git-credentials",
        "secrets.toml",
    }
)
FORBIDDEN_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".tfvars",
    ".tfvars.json",
    ".env",
)
FORBIDDEN_PATH_ENDINGS = (
    "/.aws/credentials",
    "/.docker/config.json",
    "/.kube/config",
)
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")
SSH_KEY_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_SSH_KEY_RE = re.compile(r"id_(?:rsa|dsa|ecdsa|ed25519)(?!.*\.pub\Z)")


def is_forbidden_path(path: str) -> bool:
    """True if ``path`` names a credential file that must never be sent.

    The same rules as the server's check, so the two agree on every path:
    basename matches (``.env``, ``*.pem``, ``.env.*`` except templates, SSH
    private keys) and path endings (``.aws/credentials``), ignoring case and
    accepting Windows separators. Use it on any path list the client sends.
    """
    normalized = path.replace("\\", "/").lower()
    base = normalized.rsplit("/", 1)[-1]
    if base in FORBIDDEN_NAMES or base.endswith(FORBIDDEN_SUFFIXES):
        return True
    if _SSH_KEY_RE.match(base):
        return True
    if base.startswith(".env."):
        return not base.endswith(ENV_TEMPLATE_SUFFIXES)
    return ("/" + normalized).endswith(FORBIDDEN_PATH_ENDINGS)


def exclude_pathspecs() -> list[str]:
    """Git pathspecs that leave out every forbidden path, anywhere in the tree.

    Pathspecs can't say "except templates", so ``.env.*`` and ``id_rsa*`` are
    excluded whole (``.env.example`` and ``id_rsa.pub`` too): leaving out a
    harmless file costs a little coaching, sending a secret costs trust.
    """
    patterns = [
        *(f"**/{name}" for name in sorted(FORBIDDEN_NAMES)),
        *(f"**/*{suffix}" for suffix in FORBIDDEN_SUFFIXES),
        *(f"**{ending}" for ending in FORBIDDEN_PATH_ENDINGS),
        "**/.env.*",
        *(f"**/{name}*" for name in SSH_KEY_NAMES),
    ]
    return [f":(exclude,glob,icase){pattern}" for pattern in patterns]
