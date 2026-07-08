"""Leak gate — the tree must be clean, the gate must not trip on its own
denylist, and its matching rules must behave as specified.

The denylist is base64-encoded in tests/private_strings.txt and decoded at
runtime, so neither the denylist file nor this test embeds any plaintext
private token.
"""

from pathlib import Path

from agent_ultra.leakgate import (
    scan_tree, scan_text, scan_message, load_denylist, DENYLIST_RELPATH,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DENYLIST = REPO_ROOT / DENYLIST_RELPATH


def test_whole_tree_is_clean():
    hits = scan_tree(REPO_ROOT)
    assert hits == [], f"private strings found in the tree: {hits}"


def test_denylist_decodes_to_tokens():
    tokens = load_denylist(DENYLIST)
    assert len(tokens) >= 20            # the mandated minimum set
    # every token round-trips to non-empty plaintext
    assert all(t for t in tokens)


def test_denylist_file_does_not_trip_itself():
    # The base64 denylist file is exempt and holds no plaintext, so scanning
    # the tree (which includes it) yields no hits from it.
    hits = [h for h in scan_tree(REPO_ROOT) if h[0] == DENYLIST_RELPATH]
    assert hits == []


# The case-sensitive whole-word token, reconstructed from base64 so this test
# file embeds no plaintext private string (and so stays clean under the gate).
import base64 as _b64
_CS_WORD = _b64.b64decode("U09VTA==").decode("utf-8")   # the all-caps collision


def test_case_insensitive_match():
    tokens = load_denylist(DENYLIST)
    # pick a lowercase-safe token (not the case-sensitive whole-word one)
    ci_tok = next(t for t in tokens if t != _CS_WORD and t.isascii())
    assert scan_text(ci_tok.upper(), tokens)      # uppercased still matches
    assert scan_text(ci_tok.lower(), tokens)


def test_soul_is_case_sensitive_whole_word():
    tokens = load_denylist(DENYLIST)
    assert _CS_WORD in tokens
    # exact all-caps whole word trips
    assert any(tok == _CS_WORD
               for _, tok in scan_text(f"the {_CS_WORD} of it", tokens))
    # lowercase prose does NOT trip (common English word)
    assert not any(tok == _CS_WORD
                   for _, tok in scan_text(f"a gentle {_CS_WORD.lower()} by",
                                           tokens))
    # substring inside another word does NOT trip
    assert not any(tok == _CS_WORD
                   for _, tok in scan_text(f"{_CS_WORD}FUL music", tokens))


def test_path_separator_normalised():
    tokens = load_denylist(DENYLIST)
    # a windows-path token written with forward slashes still matches
    win_tok = next((t for t in tokens if "\\" in t), None)
    assert win_tok is not None
    posix_form = win_tok.replace("\\", "/")
    assert scan_text(posix_form, tokens)


def test_scan_message_catches_token():
    tokens = load_denylist(DENYLIST)
    a_tok = next(t for t in tokens if t != _CS_WORD)
    hits = scan_message(f"fix: touched {a_tok} config", DENYLIST)
    assert any(tok == a_tok for _, tok in hits)


def test_clean_message_passes():
    assert scan_message("phase8: add receipts_bus and verifier", DENYLIST) == []
