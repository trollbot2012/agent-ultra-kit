"""Router worker — the default, stdlib, single-call fixer.

Given a confirmed finding + the workspace source, it asks the configured
model for ONE corrected file and returns it as an advisory edit. The loop
applies the edit and re-runs tests (rolling back if they break), so the
router worker's output still passes through every gate. It has no from-scratch
builder — building a multi-file project from nothing is where the optional
Deep Agents worker earns its keep (see the A/B decision).
"""

from __future__ import annotations

from pathlib import Path

from ..panel.json_extract import extract_json
from .result import WorkerResult, failed

_SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
                ".rb", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php",
                ".sh", ".sql", ".yaml", ".yml", ".toml", ".json"}
_SKIP = {".git", ".ultra", "node_modules", "__pycache__", ".venv", "venv"}


class RouterWorker:
    name = "router"

    def __init__(self, client, model: str, max_tokens: int = 8000,
                 max_file_chars: int = 6000, max_files: int = 40):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.max_file_chars = max_file_chars
        self.max_files = max_files

    # -- source ------------------------------------------------------------

    def _source_map(self, workspace: Path, prefer=None) -> dict:
        prefer = {str(p).replace("\\", "/") for p in (prefer or [])}
        files, out = [], {}
        for p in sorted(workspace.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(workspace)
            if any(part in _SKIP for part in rel.parts):
                continue
            if p.suffix.lower() in _SOURCE_EXTS:
                files.append(p)
        files.sort(key=lambda p: (str(p.relative_to(workspace)).replace(
            "\\", "/") not in prefer, str(p)))
        for p in files[:self.max_files]:
            try:
                out[str(p.relative_to(workspace)).replace("\\", "/")] = \
                    p.read_text(encoding="utf-8", errors="replace")[:self.max_file_chars]
            except OSError:
                continue
        return out

    # -- worker API --------------------------------------------------------

    def build(self, workspace, task: str, context: dict | None = None) -> WorkerResult:
        # The router worker deliberately has no from-scratch builder — a single
        # model call cannot reliably scaffold a multi-file project. Ultra's
        # auto policy routes builder tasks to Deep Agents when available.
        return failed("router",
                      "router worker has no builder; use --builder deepagents "
                      "for from-scratch multi-file work",
                      summary="no builder in the router worker")

    def fix(self, workspace, fix_task: dict, context: dict | None = None) -> WorkerResult:
        ws = Path(workspace)
        src = (context or {}).get("source_map") or self._source_map(
            ws, (context or {}).get("changed_files"))
        files_block = "\n".join(f"### {rel}\n{content}"
                                for rel, content in src.items())
        prompt = (
            "FIX: a confirmed production-safety finding must be fixed in this "
            "codebase WITHOUT breaking the existing tests.\n"
            f"Finding ({fix_task.get('severity')}/{fix_task.get('verdict')}, "
            f"lens {fix_task.get('lens')}): {fix_task.get('claim')}\n"
            + (f"Proof check: {fix_task.get('check')}\n"
               if fix_task.get('check') else "")
            + f"\nSource files:\n{files_block}\n\n"
            'Reply with ONLY a JSON object: {"path": "<relative path of the '
            'ONE file to change>", "content": "<the FULL corrected file '
            'content>", "explanation": "<one sentence>"}. Keep the public API '
            "and imports stable so the existing tests still pass. Fix the root "
            "cause, not a symptom.")
        try:
            raw = self.client.complete(self.model, prompt, self.max_tokens)
        except Exception as e:
            return failed("router", f"{type(e).__name__}: {e}",
                          summary="model call failed")
        obj = extract_json(raw)
        if not isinstance(obj, dict) or not obj.get("path") or not obj.get("content"):
            return failed("router", "model returned no usable edit",
                          summary="no edit produced")
        rel = str(obj["path"]).strip().replace("\\", "/")
        return WorkerResult(
            worker="router", status="ok",
            summary=str(obj.get("explanation", "router single-file fix"))[:200],
            files_changed=[rel],
            commands_run=[f"model:{self.model} (1 call)"],
            proof=[f"advisory edit for {rel}"],
            edit={"path": rel, "content": str(obj["content"])})
