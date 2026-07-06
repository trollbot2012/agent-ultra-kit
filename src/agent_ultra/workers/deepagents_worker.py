"""Deep Agents worker — OPTIONAL heavy builder/fixer.

Wraps LangChain Deep Agents (``create_deep_agent``) as a multi-step worker
for from-scratch multi-file builds and large repairs — the cases where the
single-call router worker's ``builder=None`` is a real limitation.

NOT imported at package import time. The langchain/deepagents import lives
inside ``_require_deepagents`` so ``import agent_ultra`` and the whole default
install stay zero-dependency. Selecting this worker without the optional extra
raises a clear install error:

    pip install agent-ultra-kit[deepagents]

Safety: this worker edits files and runs tests INSIDE the workspace, but it is
still only a worker. Its output flows back through Ultra's test runner, panel,
proof manifest, and (in an embedding host) the command broker / gates. Deep
Agents never decides what ships — Ultra does.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .result import WorkerResult, failed

_INSTALL_HINT = ("Deep Agents worker requires the optional extra: "
                 "pip install agent-ultra-kit[deepagents]")


def _require_deepagents():
    try:
        from deepagents import create_deep_agent  # noqa: F401
        from langchain_openai import ChatOpenAI    # noqa: F401
    except ImportError as e:
        raise RuntimeError(f"{_INSTALL_HINT}  (import error: {e})") from e
    return create_deep_agent, ChatOpenAI


def deepagents_available() -> bool:
    try:
        _require_deepagents()
        return True
    except RuntimeError:
        return False


class DeepAgentsWorker:
    name = "deepagents"

    def __init__(self, base_url: str, api_key: str, model: str,
                 max_tokens: int = 4000, timeout: int = 180,
                 recursion_limit: int = 40):
        # Fail loudly at construction if the extra is missing.
        self._create_deep_agent, self._ChatOpenAI = _require_deepagents()
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.recursion_limit = recursion_limit

    def _model(self):
        return self._ChatOpenAI(
            model=self.model, base_url=self.base_url, api_key=self.api_key,
            use_responses_api=False,   # required for LiteLLM-style proxies
            temperature=0, max_tokens=self.max_tokens, timeout=self.timeout)

    def _agent(self, ws: Path, touched: dict, system_prompt: str):
        # crash-proof tools — a raising tool kills the agent loop. langchain
        # also REQUIRES a docstring on every tool function.
        def read_file(path: str) -> str:
            """Read and return the full text of a workspace file."""
            try:
                return (ws / path).read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return f"ERROR reading {path}: {e}"

        def write_file(path: str, content: str) -> str:
            """Overwrite a workspace file with new content."""
            try:
                target = (ws / path).resolve()
                if not str(target).startswith(str(ws.resolve())):
                    return f"ERROR: {path} is outside the workspace"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                touched.setdefault("files", [])
                if path not in touched["files"]:
                    touched["files"].append(path)
                return f"wrote {len(content)} chars to {path}"
            except Exception as e:
                return f"ERROR writing {path}: {e}"

        def run_tests(cmd: str = "") -> str:
            """Run the test command and return exit code plus output."""
            try:
                c = cmd or touched.get("test_cmd") or "python -m pytest -q"
                r = subprocess.run(c, cwd=str(ws), shell=True,
                                   capture_output=True, text=True, timeout=120)
                touched.setdefault("commands", []).append(c)
                return f"exit={r.returncode}\n{(r.stdout + r.stderr)[-2500:]}"
            except Exception as e:
                return f"ERROR running tests: {e}"

        def list_files(_ignored: str = "") -> str:
            """List python files in the workspace (relative paths)."""
            try:
                return "\n".join(sorted(
                    str(p.relative_to(ws)) for p in ws.rglob("*.py")
                    if "__pycache__" not in str(p)))
            except Exception as e:
                return f"ERROR: {e}"

        return self._create_deep_agent(
            model=self._model(),
            tools=[read_file, write_file, run_tests, list_files],
            system_prompt=system_prompt)

    def _invoke(self, ws: Path, touched: dict, task_text: str) -> str | None:
        agent = self._agent(ws, touched, touched["system_prompt"])
        try:
            agent.invoke({"messages": [{"role": "user", "content": task_text}]},
                         {"recursion_limit": self.recursion_limit})
            return None
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    # -- worker API --------------------------------------------------------

    def build(self, workspace, task: str, context: dict | None = None) -> WorkerResult:
        ws = Path(workspace)
        touched = {"files": [], "commands": [],
                   "test_cmd": (context or {}).get("test_cmd", ""),
                   "system_prompt": (
                       "You are a multi-file build worker inside a supervised "
                       "loop. Implement the task by creating/editing files with "
                       "write_file, then run_tests to confirm it works. Keep it "
                       "minimal and dependency-free. Stop once tests pass.")}
        t0 = time.time()
        err = self._invoke(ws, touched, f"Build this: {task}")
        secs = round(time.time() - t0, 1)
        if err:
            return failed("deepagents", err,
                          summary=f"build failed after {secs}s")
        return WorkerResult(
            worker="deepagents", status="ok",
            summary=f"multi-step build ({secs}s)",
            files_changed=touched["files"], commands_run=touched["commands"],
            proof=[f"{len(touched['files'])} file(s) written"])

    def fix(self, workspace, fix_task: dict, context: dict | None = None) -> WorkerResult:
        ws = Path(workspace)
        src = (context or {}).get("source_map") or {}
        files_block = "\n".join(f"### {r}\n{c[:4000]}" for r, c in src.items())
        touched = {"files": [], "commands": [],
                   "test_cmd": (context or {}).get("test_cmd", ""),
                   "system_prompt": (
                       "You are a code-fixing worker inside a supervised loop. "
                       "Fix the ONE reported defect by editing files with "
                       "write_file, keeping the public API stable, then "
                       "run_tests to confirm ALL tests still pass. Fix the root "
                       "cause. Stop once tests pass.")}
        task_text = (
            f"Defect ({fix_task.get('severity')}/{fix_task.get('verdict')}, "
            f"lens {fix_task.get('lens')}): {fix_task.get('claim')}\n"
            + (f"Proof check: {fix_task.get('check')}\n"
               if fix_task.get('check') else "")
            + (f"\nWorkspace files:\n{files_block}\n" if files_block else "")
            + "\nFix it, then run_tests to prove the suite still passes.")
        t0 = time.time()
        err = self._invoke(ws, touched, task_text)
        secs = round(time.time() - t0, 1)
        if err:
            return failed("deepagents", err, summary=f"fix failed after {secs}s")
        if not touched["files"]:
            return failed("deepagents", "agent made no edit",
                          summary="no edit produced")
        last = touched["files"][-1]
        try:
            content = (ws / last).read_text(encoding="utf-8")
        except OSError as e:
            return failed("deepagents", f"could not read {last}: {e}")
        # return the primary changed file as an advisory edit so the loop's
        # apply+rollback still governs (same contract as the router worker).
        return WorkerResult(
            worker="deepagents", status="ok",
            summary=f"multi-step fix ({secs}s)",
            files_changed=touched["files"], commands_run=touched["commands"],
            proof=[f"edited {len(touched['files'])} file(s)"],
            edit={"path": last, "content": content})
