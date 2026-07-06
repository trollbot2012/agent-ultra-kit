"""Adapt a worker to UltraLoop's builder/fixer seams.

UltraLoop's seams are ``fixer(fix_task, workspace) -> bool`` and
``builder(workspace, task) -> str``. A worker returns a :class:`WorkerResult`
with an advisory edit; these adapters apply the edit, re-run tests, roll back
on breakage, and record the worker result to the artifact dir — so a worker's
change still passes the test gate before the loop's re-panel sees it. The last
WorkerResult per fix is captured on the returned callable's ``.results`` list
for reporting.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path


def _as_dict(fix_task) -> dict:
    """The loop passes a FixTask dataclass; workers take a plain dict."""
    if is_dataclass(fix_task) and not isinstance(fix_task, type):
        return asdict(fix_task)
    if isinstance(fix_task, dict):
        return fix_task
    # last resort: pull known attributes
    return {k: getattr(fix_task, k, "") for k in
            ("id", "lens", "severity", "verdict", "claim", "check")}


def worker_as_fixer(worker, *, test_runner, test_cmd, timeout,
                    source_provider=None, art_dir=None):
    """Return a ``fixer(fix_task, workspace) -> bool`` backed by *worker*.

    Applies the worker's advisory edit, re-runs tests, and reverts if they
    break (a fix that breaks the contract is not a fix). Records every
    WorkerResult so the loop can surface which worker did what.
    """
    results: list = []

    def fixer(fix_task, workspace) -> bool:
        ws = Path(workspace)
        ft = _as_dict(fix_task)
        ctx = {"test_cmd": test_cmd}
        if source_provider is not None:
            ctx["source_map"] = source_provider(ws, ft)
        res = worker.fix(ws, ft, ctx)
        results.append(res)
        _archive(art_dir, ft, res)
        if not res.ok or not res.edit or not res.edit.get("path"):
            return False
        rel = res.edit["path"]
        target = (ws / rel).resolve()
        try:
            if not str(target).startswith(str(ws.resolve())):
                res.status, res.error = "failed", "edit outside workspace"
                return False
        except ValueError:
            return False
        backup = target.read_text(encoding="utf-8") if target.exists() else None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(res.edit["content"], encoding="utf-8")
        except OSError as e:
            res.status, res.error = "failed", f"write failed: {e}"
            return False
        tr = test_runner(ws, test_cmd, timeout)
        if tr.passed:
            res.proof.append(f"tests pass after fix ({tr.cmd} exit {tr.exit_code})")
            return True
        # revert — broke the suite
        if backup is not None:
            try:
                target.write_text(backup, encoding="utf-8")
            except OSError:
                pass
        res.status = "failed"
        res.error = "reverted: fix broke the test suite"
        _archive(art_dir, fix_task, res, suffix="-reverted")
        return False

    fixer.results = results   # type: ignore[attr-defined]
    fixer.worker_name = getattr(worker, "name", "worker")  # type: ignore
    return fixer


def worker_as_builder(worker, *, test_cmd="", art_dir=None):
    """Return a ``builder(workspace, task) -> str`` backed by *worker*."""
    results: list = []

    def builder(workspace, task: str) -> str:
        res = worker.build(Path(workspace), task, {"test_cmd": test_cmd})
        results.append(res)
        _archive(art_dir, {"id": "BUILD"}, res)
        return res.summary

    builder.results = results   # type: ignore[attr-defined]
    builder.worker_name = getattr(worker, "name", "worker")  # type: ignore
    return builder


def _archive(art_dir, task: dict, res, suffix: str = "") -> None:
    if not art_dir:
        return
    try:
        p = Path(art_dir) / f"worker-{task.get('id', 'x')}{suffix}.json"
        p.write_text(json.dumps(res.to_dict(), indent=2), encoding="utf-8")
    except OSError:
        pass
