"""Evidence reader — bounded, redacted source gathering for panels.

A panel that debates without evidence debates fiction. This module turns
files/directories into a bounded context blob, flags low-context runs, and
scrubs secrets so evidence can be logged and shipped in artifacts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_EXTS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
    ".java", ".kt", ".rb", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs",
    ".php", ".swift", ".scala", ".clj", ".ex", ".exs", ".sql", ".sh", ".bash",
    ".ps1", ".psm1", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".proto", ".graphql", ".tf", ".md", ".rst", ".txt",
})
SKIP_DIRS = frozenset({
    ".git", ".ultra", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "build", ".mypy_cache", ".pytest_cache", ".tox", "target", ".idea",
    ".egg-info",
})

LOW_CONTEXT_THRESHOLD = 500

# Secret shapes worth scrubbing from anything we log or ship. Values are
# replaced; key names are kept so the reader can still see WHAT was redacted.
_SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[bapros]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{16,}"),
]
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|secret|passwd|password|authorization)"
    r"(\s*[:=]\s*)(['\"]?)[^\s'\"]{8,}\3")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL)

REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Scrub common secret shapes. Keeps key names, replaces values."""
    if not text:
        return text or ""
    out = _PEM_RE.sub(REDACTED, text)
    for rx in _SECRET_RES:
        out = rx.sub(REDACTED, out)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return out


def find_secrets(text: str) -> list:
    """Return the secret-shaped substrings found in *text* (for scanners;
    redact_secrets is the writer-side counterpart)."""
    if not text:
        return []
    hits = [m.group(0) for m in _PEM_RE.finditer(text)]
    for rx in _SECRET_RES:
        hits.extend(m.group(0) for m in rx.finditer(text))
    hits.extend(m.group(0) for m in _KV_SECRET_RE.finditer(text)
                if REDACTED not in m.group(0))
    return hits


def is_low_context(text: str, threshold: int = LOW_CONTEXT_THRESHOLD) -> bool:
    return len((text or "").strip()) < threshold


@dataclass
class Evidence:
    text: str = ""
    files: list = field(default_factory=list)
    truncated: bool = False

    @property
    def low_context(self) -> bool:
        return is_low_context(self.text)


def _truncate(text: str, limit: int, marker: str = "\n...[excerpt cut]") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def _looks_binary(path: Path) -> bool:
    try:
        return b"\x00" in path.open("rb").read(2048)
    except OSError:
        return True


def gather(paths=(), dirs=(), max_files: int = 40,
           max_file_chars: int = 8000, max_total_chars: int = 24000,
           exts=SOURCE_EXTS, redact: bool = True) -> Evidence:
    """Read explicit *paths* plus recursive *dirs* into one bounded blob.

    Only files with an allowlisted extension are read from directories;
    explicit paths are read regardless of extension (the caller asked).
    Binary files and skip-dirs are ignored. Never raises on unreadable files.
    """
    ev = Evidence()
    seen: set[Path] = set()
    candidates: list[Path] = []
    for p in paths:
        try:
            candidates.append(Path(p).resolve())
        except OSError:
            continue
    for d in dirs:
        try:
            root = Path(d).resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if len(candidates) >= max_files * 4:
                break
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            candidates.append(f)

    blocks: list[str] = []
    total = 0
    for f in candidates:
        if len(ev.files) >= max_files or total >= max_total_chars:
            ev.truncated = True
            break
        if f in seen or not f.is_file() or _looks_binary(f):
            continue
        seen.add(f)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if redact:
            text = redact_secrets(text)
        excerpt = _truncate(text, min(max_file_chars, max_total_chars - total))
        blocks.append(f"--- {f.name} ---\n{excerpt}")
        ev.files.append(str(f))
        total += len(excerpt)
    ev.text = "\n\n".join(blocks)
    if len(ev.text) > max_total_chars:
        ev.text = _truncate(ev.text, max_total_chars, "\n...[evidence cut]")
        ev.truncated = True
    return ev
