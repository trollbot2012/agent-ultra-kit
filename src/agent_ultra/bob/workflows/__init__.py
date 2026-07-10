"""Bundled ultracode workflow scripts used by bob steps 6 and 7."""

from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parent

SECURITY_WORKFLOW = WORKFLOWS_DIR / "security_fanout.py"
REVIEW_WORKFLOW = WORKFLOWS_DIR / "review_fanout.py"
SURGICAL_REVIEW_WORKFLOW = WORKFLOWS_DIR / "surgical_review.py"
