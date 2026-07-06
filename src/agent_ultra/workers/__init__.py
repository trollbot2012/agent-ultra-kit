"""Ultra worker layer — pluggable builder/fixer runtimes.

Ultra stays the supervisor (build -> test -> panel -> classify -> fix ->
re-test -> re-panel -> proof gate). A *worker* only fills the builder and/or
fixer slots. Two are shipped:

- ``RouterWorker`` — the default. Stdlib-only, one model call per fix, returns
  an advisory single-file edit. Cheap, portable, Windows-native, proof-gate
  compatible. No dependencies beyond the kit itself.
- ``DeepAgentsWorker`` — OPTIONAL. Wraps LangChain Deep Agents as a
  multi-step builder/fixer for from-scratch multi-file work. Imported only
  when selected; requires ``pip install agent-ultra-kit[deepagents]``.

Both normalize to :class:`WorkerResult` so the loop never cares which worker
produced a change — only whether tests + panel + proof accept it. A worker
NEVER bypasses the gates: its edits still flow through the test runner, the
panel, and the proof manifest before Ultra decides to ship.
"""

from .result import WorkerResult
from .router_worker import RouterWorker
from .select import select_worker, AUTO_POLICY

__all__ = ["WorkerResult", "RouterWorker", "select_worker", "AUTO_POLICY"]
