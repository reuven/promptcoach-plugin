"""Hypothesis properties for the before-prompt path, and the profile choice."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import conftest
from conftest import TOKEN
from promptcoach_client import core
from promptcoach_client.claude_code import PROFILE
from promptcoach_client.contract import COMMAND_SYNONYMS

# --- the profile choice -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "cwd", "expected"),
    [
        ({}, "/x/project", "dev"),
        ({"CI": "1"}, "/x/project", "ci"),
        ({}, "/x/mutants", "mutmut"),
        ({"CI": "1"}, "/x/mutants", "mutmut"),
        ({"HYPOTHESIS_PROFILE": "ci"}, "/x/mutants", "ci"),
        ({"HYPOTHESIS_PROFILE": "dev", "CI": "1"}, "/x/project", "dev"),
    ],
)
def test_profile_choice_order(env: dict[str, str], cwd: str, expected: str) -> None:
    """HYPOTHESIS_PROFILE, then mutmut inside ``mutants/``, then ci on CI, then dev."""
    assert conftest.hypothesis_profile(env, Path(cwd)) == expected


def test_profiles_are_sized_for_their_use() -> None:
    ci = settings.get_profile("ci")
    assert ci.max_examples == 200
    assert ci.derandomize and ci.deadline is None and ci.database is None
    assert settings.get_profile("dev").max_examples == 25
    mutmut = settings.get_profile("mutmut")
    assert (mutmut.max_examples, mutmut.derandomize, mutmut.deadline) == (10, True, None)


# --- strategies ---------------------------------------------------------------------------

RTL_MARKS = ["\u200f", "\u202b", "\u202e", "\u2067", "\u061c"]
_text = st.text(st.characters(categories=["L", "M", "P", "Z", "Cc", "Cf"]), max_size=200)
_nfd = _text.map(lambda s: unicodedata.normalize("NFD", s))
_rtl = st.lists(st.one_of(_text, st.sampled_from(RTL_MARKS)), max_size=8).map("".join)

# Coach commands, varied the ways the detector allows: case, spacing, separator,
# "please", trailing punctuation. Only those it accepts are kept.
_commands = st.builds(
    lambda coach, sep, please, phrase, tail, pad: f"{pad}{coach}{sep}{please}{phrase}{tail}{pad}",
    st.sampled_from(["coach", "Coach", "COACH", "cOaCh"]),
    st.sampled_from([", ", ": ", " ", ",", ":\t", "  "]),
    st.sampled_from(["", "please ", "PLEASE "]),
    st.sampled_from(sorted(COMMAND_SYNONYMS)).flatmap(
        lambda p: st.sampled_from([p, p.upper(), p.title(), p.replace("'", "\u2019")])
    ),
    st.sampled_from(["", ".", "!", "?", " please", "!!"]),
    st.sampled_from(["", " ", "\n", "  \t"]),
).filter(core.is_coach_command)

SERVER_MESSAGES = [
    "Prompt coach: sign in at https://coach.lernerpython.com/signin to get your token, "
    "and coaching will start right away.",
    "Prompt coach: your LernerPython membership isn't active right now. "
    "Renew at https://lernerpython.com and coaching picks right back up.",
]
_non_blocking = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "message": st.one_of(st.none(), st.sampled_from(SERVER_MESSAGES)),
            "block_prompt": st.just(False),
            "prompt_only": st.booleans(),
        }
    ),
)
# Anything a server (or something pretending to be one) might send back.
_any_reply = st.one_of(
    _non_blocking,
    st.fixed_dictionaries(
        {
            "message": st.one_of(st.none(), _text),
            "block_prompt": st.one_of(st.booleans(), st.integers(), st.none()),
            "prompt_only": st.one_of(st.booleans(), st.text(max_size=3)),
        }
    ),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
)


@pytest.fixture(scope="module")
def dirs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """An empty project and home, shared by every example (nothing writes to them)."""
    root = tmp_path_factory.mktemp("props")
    (root / "project").mkdir()
    (root / "home").mkdir()
    return root / "project", root / "home"


def _before(
    prompt: str, reply: dict[str, Any] | None, dirs: tuple[Path, Path], token: str | None = TOKEN
) -> core.Reply | None:
    """``before()`` with the server's answer replaced by ``reply``: no network, no git."""
    project, home = dirs
    config = core.Config(token, "https://coach.invalid", True, None, home)
    event = core.PromptSubmitted("sess-1", "prompt-1", prompt, project, project)
    with mock.patch.object(core, "post", return_value=reply):
        return core.before(event, config, PROFILE)


# --- properties ---------------------------------------------------------------------------

_SLOW = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


@_SLOW
@given(
    prompt=st.one_of(_text, _nfd, _rtl, _commands, st.tuples(_rtl, _commands).map("".join)),
    reply=_any_reply,
    has_token=st.booleans(),
)
def test_before_never_crashes(
    prompt: str, reply: dict[str, Any] | None, has_token: bool, dirs: tuple[Path, Path]
) -> None:
    """Any prompt, any reply: a Reply or None, and a command is always blocked."""
    result = _before(prompt, reply, dirs, TOKEN if has_token else None)

    assert result is None or isinstance(result, core.Reply)
    if core.is_coach_command(prompt):
        assert result is not None and result.block and result.message


@_SLOW
@given(prompt=_commands, reply=_non_blocking)
def test_command_with_a_non_blocking_reply_blocks_without_echo(
    prompt: str, reply: dict[str, Any] | None, dirs: tuple[Path, Path]
) -> None:
    result = _before(prompt, reply, dirs)

    assert result is not None and result.block and result.message
    server_message = reply["message"] if reply else None
    assert result.message == (server_message or core.COMMAND_NOT_ANSWERED)
    # The reason doesn't depend on what was typed: any other command gets the same.
    assert result == _before("coach, quieter", reply, dirs)
    assert " ".join(prompt.split()).casefold() not in result.message.casefold()


@_SLOW
@given(prompt=st.one_of(_text, _nfd, _rtl), reply=_non_blocking)
def test_ordinary_prompt_is_never_blocked_locally(
    prompt: str, reply: dict[str, Any] | None, dirs: tuple[Path, Path]
) -> None:
    """Fail open for everything else: exactly the server's reply, never a local block."""
    assume(not core.is_coach_command(prompt))

    assert _before(prompt, reply, dirs) == core.parse_reply(reply)
