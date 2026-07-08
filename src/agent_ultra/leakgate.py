"""Leak gate — scan the tracked tree (and commit messages / PR bodies) for
private strings before any release tag.

The denylist lives in ``tests/private_strings.txt`` as base64, one token per
line. It is decoded at RUNTIME so the denylist file itself never contains the
plaintext tokens and therefore does not trip its own scan. There is no file
whitelist by design: a hit is a hit, and the fix is to change the offending
text, not to exempt a file.

Matching rules (from the backport spec §0):

  * Case-insensitive for every token EXCEPT the one that collides with a
    common English word — that token is matched case-sensitively and only as a
    whole word, so ordinary prose using the lowercase word does not trip it.
  * Path separators are normalised (``\\`` -> ``/``) in both the haystack and
    the needles before matching, so a Windows-style path token matches its
    POSIX-written form and vice versa.

Usage:
    from agent_ultra.leakgate import scan_tree, load_denylist
    hits = scan_tree(repo_root)          # [(path, line_no, token), ...]

CLI:
    agent-ultra-leakgate [REPO_ROOT] [--message "commit msg"]
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

# Tokens matched whole-word and CASE-SENSITIVELY (they collide with common
# English words), identified by their decoded UPPER-CASE form. Stored
# base64-encoded so this source file holds no private plaintext and does not
# trip its own scan. Decode: base64 of the all-caps English-word collision.
_CASE_SENSITIVE_WHOLE_WORD_B64 = frozenset({"U09VTA=="})
_CASE_SENSITIVE_WHOLE_WORD = frozenset(
    base64.b64decode(b).decode("utf-8") for b in _CASE_SENSITIVE_WHOLE_WORD_B64)

# Directories never worth scanning (build/VCS noise, not source).
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", ".venv",
    "venv", "node_modules", "dist", "build", ".egg-info", ".idea", ".ruff_cache",
})

# Binary-ish extensions we do not read as text.
_SKIP_EXTS = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".png", ".jpg", ".jpeg", ".gif",
    ".ico", ".pdf", ".zip", ".gz", ".whl", ".woff", ".woff2", ".ttf",
})

DENYLIST_RELPATH = "tests/private_strings.txt"


def _norm(text: str) -> str:
    """Normalise path separators so \\ and / forms match interchangeably."""
    return text.replace("\\", "/")


def load_denylist(denylist_path: str | Path) -> list[str]:
    """Decode the base64 denylist into plaintext tokens. Ignores comments and
    blank lines. Raises if the file is missing (a silent empty denylist would
    make the gate a no-op)."""
    p = Path(denylist_path)
    tokens: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens.append(base64.b64decode(line).decode("utf-8"))
    if not tokens:
        raise ValueError(f"denylist {p} decoded to zero tokens")
    return tokens


def _build_matchers(tokens: list[str]):
    """Return (ci_needles, cs_word_patterns).

    ci_needles       — normalised, lowercased plain substrings (case-insensitive).
    cs_word_patterns — (token, compiled regex) for whole-word case-sensitive.
    """
    ci: list[tuple[str, str]] = []      # (original_token, normalized_lower)
    cs: list[tuple[str, re.Pattern]] = []
    for tok in tokens:
        if tok in _CASE_SENSITIVE_WHOLE_WORD:
            cs.append((tok, re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tok)}"
                                       rf"(?![A-Za-z0-9_])")))
        else:
            ci.append((tok, _norm(tok).lower()))
    return ci, cs


def scan_text(text: str, tokens: list[str]) -> list[tuple[int, str]]:
    """Scan a blob of text. Returns ``[(line_no, token), ...]`` (1-based)."""
    ci, cs = _build_matchers(tokens)
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        norm_lower = _norm(line).lower()
        for original, needle in ci:
            if needle and needle in norm_lower:
                hits.append((i, original))
        for original, pat in cs:
            if pat.search(line):    # case-sensitive, whole-word, un-normalised
                hits.append((i, original))
    return hits


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() in _SKIP_EXTS:
            continue
        yield p


def scan_tree(root: str | Path,
              denylist_path: str | Path | None = None) -> list[tuple[str, int, str]]:
    """Scan every text file under ``root`` (minus VCS/build noise). Returns
    ``[(relpath, line_no, token), ...]``. The denylist file is decoded and its
    own lines are exempt from matching (they are base64, not plaintext)."""
    root = Path(root)
    denylist_path = Path(denylist_path or (root / DENYLIST_RELPATH))
    tokens = load_denylist(denylist_path)
    denylist_resolved = denylist_path.resolve()
    hits: list[tuple[str, int, str]] = []
    for p in _iter_files(root):
        if p.resolve() == denylist_resolved:
            continue    # the base64 denylist is exempt (it holds no plaintext)
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue    # binary or unreadable: skip
        for line_no, token in scan_text(text, tokens):
            hits.append((str(p.relative_to(root)).replace("\\", "/"),
                         line_no, token))
    return hits


def scan_message(message: str,
                 denylist_path: str | Path) -> list[tuple[int, str]]:
    """Scan a commit message / PR body string."""
    return scan_text(message, load_denylist(denylist_path))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="agent-ultra-leakgate",
                                 description="scan the tree for private strings")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--denylist", default="")
    ap.add_argument("--message", default="",
                    help="also scan this commit message / PR body")
    args = ap.parse_args(argv)
    root = Path(args.root)
    denylist = args.denylist or str(root / DENYLIST_RELPATH)
    tree_hits = scan_tree(root, denylist)
    msg_hits = scan_message(args.message, denylist) if args.message else []
    if not tree_hits and not msg_hits:
        print("leak gate: PASS (no private strings found)")
        return 0
    for path, line_no, token in tree_hits:
        print(f"LEAK {path}:{line_no}: private token {token!r}")
    for line_no, token in msg_hits:
        print(f"LEAK <commit-message>:{line_no}: private token {token!r}")
    print(f"\nleak gate: FAIL ({len(tree_hits) + len(msg_hits)} hit(s))")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
