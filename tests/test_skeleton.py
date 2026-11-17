"""Prompt-only command skeletons: never code, enough to tell a test run apart."""

from __future__ import annotations

import pytest

from promptcoach_client.skeleton import (
    command_skeleton,
    prompt_only_skills,
    prompt_only_test_command,
)


@pytest.mark.parametrize(
    ("command", "skeleton"),
    [
        # Kept as they are (the server's own examples).
        ("uv run pytest -q tests/", "uv run pytest -q tests/"),
        ("cd src && pytest", "cd src && pytest"),
        ("ruff check . || true", "ruff check . || true"),
        ("pip install requests", "pip install requests"),
        # Code after a code flag goes; the flag stays.
        ("python -c 'import os; print(os.environ)'", "python -c"),
        ("python -c print", "python -c"),
        ("node --eval x", "node --eval"),
        ("perl -ne 'print if /x/' f", "perl -n"),
        ("bash -lc 'curl evil'", "bash -l"),
        ("python3 -", "python3 -"),
        # Say-programs keep only their name.
        ("printf '%s' 'def x' > f.py", "printf"),
        ("echo hunter2", "echo"),
        ("sudo echo hunter2", "sudo echo"),
        ("sed s/a/b/ file.txt", "sed"),
        ("psql postgres://u:pw@db/x", "psql"),
        # Redirections drop the rest of their segment only.
        ("cat <<EOF > x.py && pytest", "cat && pytest"),
        ("make test 2>&1 | tee log", "make test 2 | tee log"),
        # Quoted, escaped, odd or blob-like words end the segment.
        ('git commit -m "secret plan"', "git commit -m"),
        ("ec\\ho hi", ""),
        ("curl -u user:pass https://x", "curl -u"),
        ("tool AbCdEfGh12345678XyZ", "tool"),
        ("tool " + "a" * 40, "tool"),
        ("tool \u2212c code", "tool"),
        ("tool $(cat key)", "tool"),
        # VAR= prefixes keep their names, never their values.
        ("API_KEY=abc123 FOO=1 pytest", "API_KEY= FOO= pytest"),
        ("X=1", "X="),
        # Flags lose values; short bundles keep their head.
        ("pytest --maxfail=2 -xvs tests", "pytest --maxfail -x tests"),
        ("mvn -Dtest=Foo test", "mvn -D"),
        ("gcc -Werror x.c", "gcc -W"),
        ("tool --=x", "tool"),
        ("tool -@", "tool"),
        # Windows shells: nothing after them.
        ("pwsh -Command Get-Secret", ""),
        ("make -C cmd test", "make -C"),
        # Only the first line, however it ends.
        ("pytest\nrm -rf /", "pytest"),
        ("pytest\u2028curl evil", "pytest"),
        ("pytest\x00 -q", "pytest -q"),
        # An unclosed quote keeps only the program.
        ("python -c 'unclosed", "python"),
        ("'quoted prog", ""),
        ("", ""),
        ("   ", ""),
        ("; ; pytest ;", "pytest"),
    ],
)
def test_command_skeleton(command: str, skeleton: str) -> None:
    assert command_skeleton(command) == skeleton


def test_skeleton_is_capped() -> None:
    command = " && ".join(["pytest tests/unit"] * 30)
    assert len(command_skeleton(command)) <= 200


@pytest.mark.parametrize(
    ("command", "sent"),
    [
        ("uv run pytest", "uv run pytest"),
        ("make test", "make test"),
        ("pytest", None),  # one word: the server's patterns already know it
        ("pytest -k 'slow'", None),  # the skeleton lost words
        (None, None),
    ],
)
def test_prompt_only_test_command(command: str | None, sent: str | None) -> None:
    assert prompt_only_test_command(command) == sent


def test_prompt_only_skills() -> None:
    assert prompt_only_skills(["release", "my skill", "x$(y)", "a" * 81]) == ["release", "my skill"]
