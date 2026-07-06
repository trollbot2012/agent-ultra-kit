"""Robust JSON extraction — models wrap JSON in prose and code fences."""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _scan_balanced(cand: str, start: int, opener: str, closer: str):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(cand)):
        ch = cand[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cand[start:i + 1])
                except Exception:
                    return None
    return None


def extract_json(text: str):
    """Fenced block first, then the balanced [..] or {..} at the earliest
    opener — so an object containing arrays wins over its own inner array."""
    if not text:
        return None
    candidates = _FENCE_RE.findall(text) + [text]
    for cand in candidates:
        cand = cand.strip()
        starts = sorted(
            (cand.find(o), o, c)
            for o, c in (("[", "]"), ("{", "}")) if cand.find(o) >= 0)
        for start, opener, closer in starts:
            parsed = _scan_balanced(cand, start, opener, closer)
            if parsed is not None:
                return parsed
    return None
