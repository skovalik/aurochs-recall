"""Unit tests for core.validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aurochs_recall.core.validation import (
    InvalidInput,
    compute_content_hash,
    normalize_whitespace,
    validate_entity_name,
    validate_file_path,
    validate_predicate_name,
    validate_query_string,
)


# ----- validate_entity_name ----------------------------------------------


def test_entity_name_normalizes_nfc():
    # NFD form: 'é' as 'e' + combining acute. Should collapse to NFC.
    nfd = "André"
    nfc = "André"
    assert validate_entity_name(nfd) == nfc


def test_entity_name_strips_whitespace():
    assert validate_entity_name("  Stefan  ") == "Stefan"


@pytest.mark.parametrize("bad", ["", "   ", "\t", "null", "None", "UNDEFINED"])
def test_entity_name_rejects_bad_inputs(bad):
    with pytest.raises(InvalidInput):
        validate_entity_name(bad)


def test_entity_name_rejects_non_string():
    with pytest.raises(InvalidInput):
        validate_entity_name(None)  # type: ignore[arg-type]


# ----- validate_query_string ---------------------------------------------


def test_query_literal_quotes_input():
    assert validate_query_string("hello world") == '"hello world"'


def test_query_literal_doubles_embedded_quotes():
    # FTS5 phrase syntax escapes " by doubling.
    assert validate_query_string('say "hi"') == '"say ""hi"""'


def test_query_literal_passes_through_special_chars():
    # parens/OR/NEAR are FTS5 operators — literal mode must NOT parse them
    assert validate_query_string("(foo OR bar)") == '"(foo OR bar)"'


def test_query_raw_passes_through():
    assert validate_query_string("foo NEAR/5 bar", mode="fts5_raw") == "foo NEAR/5 bar"


def test_query_unknown_mode_raises():
    with pytest.raises(InvalidInput):
        validate_query_string("x", mode="weird")  # type: ignore[arg-type]


# ----- validate_file_path -------------------------------------------------


def test_file_path_returns_path():
    p = validate_file_path("foo/bar.txt")
    assert isinstance(p, Path)
    assert str(p) == str(Path("foo/bar.txt"))


def test_file_path_rejects_null_byte():
    with pytest.raises(InvalidInput):
        validate_file_path("foo\x00bar")


@pytest.mark.skipif(sys.platform != "win32", reason="windows-only check")
@pytest.mark.parametrize(
    "bad",
    [
        "CON.txt",
        "nul",
        "AUX.log",
        "COM1.json",
        "lpt9",
        "PRN",
        r"C:\foo\COM5\bar.txt",
    ],
)
def test_file_path_rejects_windows_reserved(bad):
    with pytest.raises(InvalidInput):
        validate_file_path(bad)


# ----- validate_predicate_name --------------------------------------------


@pytest.mark.parametrize("good", ["WORKS_FOR", "MENTIONED_BY", "A", "X1", "X_Y_Z"])
def test_predicate_accepts_valid(good):
    assert validate_predicate_name(good) == good


@pytest.mark.parametrize(
    "bad",
    ["works_for", "WORKS-FOR", "_LEADING", "1NUMERIC", "", "Mixed"],
)
def test_predicate_rejects_invalid(bad):
    with pytest.raises(InvalidInput):
        validate_predicate_name(bad)


# ----- normalize_whitespace + compute_content_hash ------------------------


def test_normalize_whitespace_collapses_runs():
    assert normalize_whitespace("a   b\t\nc") == "a b c"


def test_normalize_whitespace_strips_ends():
    assert normalize_whitespace("\n\nfoo\n") == "foo"


def test_compute_content_hash_is_deterministic():
    h1 = compute_content_hash("human", "Hello world")
    h2 = compute_content_hash("human", "Hello world")
    assert h1 == h2


def test_compute_content_hash_is_sha256_hex():
    h = compute_content_hash("human", "abc")
    # SHA-256 hex digest is 64 chars
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_content_hash_role_matters():
    # Same content, different role → different hash. The 0x1f
    # separator is what makes this true.
    h_human = compute_content_hash("human", "Hello world")
    h_assist = compute_content_hash("assistant", "Hello world")
    assert h_human != h_assist


def test_compute_content_hash_whitespace_invariant():
    # Different internal whitespace shapes hash to the same value.
    h1 = compute_content_hash("human", "Hello world")
    h2 = compute_content_hash("human", "  Hello\t\nworld  ")
    assert h1 == h2
