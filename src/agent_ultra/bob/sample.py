"""The bundled sample build task for bob's mock mode.

Mock mode swaps the MODEL CONTENT (spec text, test file, implementation) for
these deterministic fixtures — everything else is real: pytest really runs
RED and GREEN as subprocesses, ultracode really fans out on the mock route,
the panel really executes and writes its receipt, and the gate really
validates the chain. That's what makes the no-key demo honest proof of the
machinery rather than a canned transcript.

The task: a tiny ``slugify`` helper. The stub raises, so RED genuinely fails;
the implementation genuinely passes.
"""

SAMPLE_GOAL = "add a slugify(text) helper that makes URL-safe slugs"

SAMPLE_SPEC = (
    "slugify(text) lowercases the input, replaces runs of non-alphanumeric "
    "characters with single hyphens, and strips leading/trailing hyphens. "
    "Empty or symbol-only input returns ''. Input is str; output is str."
)

TEST_FILE = "test_slug_util.py"
IMPL_FILE = "slug_util.py"

SAMPLE_TESTS = '''\
"""Contract tests for slug_util.slugify (written before the implementation)."""
from slug_util import slugify


def test_lowercases_and_hyphenates():
    assert slugify("Hello World") == "hello-world"


def test_collapses_symbol_runs():
    assert slugify("a  b!!c") == "a-b-c"


def test_strips_edge_hyphens():
    assert slugify("--Already--Slugged--") == "already-slugged"


def test_empty_and_symbol_only():
    assert slugify("") == ""
    assert slugify("!!!") == ""
'''

SAMPLE_STUB = '''\
"""URL-slug helper (stub — implementation comes after RED)."""


def slugify(text):
    raise NotImplementedError
'''

SAMPLE_IMPL = '''\
"""URL-slug helper."""
import re

_RUNS = re.compile(r"[^a-z0-9]+")


def slugify(text):
    return _RUNS.sub("-", str(text).lower()).strip("-")
'''

SAMPLE_QUIZ_QUESTIONS = [
    "What does slugify return for symbol-only input, and which test proves it?",
    "Why are runs of symbols collapsed to a single hyphen instead of one each?",
    "Which step would catch a regex that also stripped digits?",
]
