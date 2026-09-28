"""Tests for the terminal-only consent helper's paste handling (D-18).

`scripts/oauth_consent.py` never opens a browser: the user opens the printed
URL anywhere, and after consent pastes back the `http://localhost:<port>/?...`
address the browser was sent to (the page itself fails to load, nothing
listens there). `extract_code` turns that paste into the authorization code.

Test description format: test_<unit>_<condition>_<expected>.
"""

from __future__ import annotations

import pytest

from scripts.oauth_consent import ConsentError, extract_code

STATE = "state-123"


def test_extract_code_full_redirect_url_returns_code() -> None:
    pasted = f"http://localhost:43123/?state={STATE}&code=4/0Abc-def&scope=https://x"
    assert extract_code(pasted, STATE) == "4/0Abc-def"


def test_extract_code_surrounding_whitespace_is_ignored() -> None:
    pasted = f"  http://localhost:43123/?state={STATE}&code=4/0Abc  \n"
    assert extract_code(pasted, STATE) == "4/0Abc"


def test_extract_code_bare_code_is_accepted() -> None:
    assert extract_code("4/0Abc-def", STATE) == "4/0Abc-def"


def test_extract_code_state_mismatch_raises() -> None:
    pasted = "http://localhost:43123/?state=other&code=4/0Abc"
    with pytest.raises(ConsentError, match="state"):
        extract_code(pasted, STATE)


def test_extract_code_access_denied_raises() -> None:
    pasted = f"http://localhost:43123/?error=access_denied&state={STATE}"
    with pytest.raises(ConsentError, match="access_denied"):
        extract_code(pasted, STATE)


def test_extract_code_url_without_code_raises() -> None:
    with pytest.raises(ConsentError, match="code"):
        extract_code(f"http://localhost:43123/?state={STATE}", STATE)


def test_extract_code_empty_paste_raises() -> None:
    with pytest.raises(ConsentError):
        extract_code("   ", STATE)
