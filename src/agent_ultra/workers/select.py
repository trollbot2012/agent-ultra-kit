"""Worker selection — the auto policy that picks router vs Deep Agents.

Router is the default and handles the common case (one-file panel-finding
repairs, proof-gate repairs, known test failures) cheaply and portably.
Deep Agents is chosen only when the task genuinely needs a multi-step,
multi-file, from-scratch or planning-heavy runtime — and only when the
optional extra is installed. If Deep Agents is requested but unavailable,
the caller gets a clear install error rather than a silent downgrade.
"""

from __future__ import annotations

from .router_worker import RouterWorker
from .deepagents_worker import deepagents_available, _INSTALL_HINT

# keywords that signal a from-scratch / multi-file build where the router
# worker's missing builder is a real limitation
_BUILDER_KEYWORDS = (
    "build", "create", "scaffold", "implement from scratch", "new project",
    "multi-file", "multiple files", "refactor", "rewrite", "migrate",
)

AUTO_POLICY = """\
router  when: small fix, one-file patch, panel-finding repair, proof-gate
             repair, known test failure (the default)
deepagents when: a builder is required, builder would otherwise be None, a
             from-scratch multi-file feature, a high-risk multi-file task, the
             router fixer stalled/failed after N rounds, or explicitly asked
"""


def wants_builder(task: str, risk: str = "medium",
                  needs_build: bool = False) -> bool:
    """Heuristic: does this task want a from-scratch/multi-file builder?"""
    if needs_build:
        return True
    blob = (task or "").lower()
    if any(k in blob for k in _BUILDER_KEYWORDS):
        return True
    return False


def resolve_worker_choice(role: str, requested: str, task: str,
                          risk: str = "medium", needs_build: bool = False,
                          router_failures: int = 0) -> str:
    """Return the worker NAME ('router' | 'deepagents') for a role
    ('builder' | 'fixer') given the requested setting.

    *requested* is 'router', 'deepagents', or 'auto'. 'auto' applies the
    policy above. This function does NOT import Deep Agents — it only decides
    a name; construction (and the install-error) happens in the factory.
    """
    role = role.lower()
    requested = (requested or "auto").lower()
    if requested in ("router", "deepagents"):
        return requested
    # auto
    if role == "builder":
        return "deepagents" if wants_builder(task, risk, needs_build) else "router"
    # fixer: router by default; escalate to deepagents if it kept failing or
    # the task is an explicitly multi-file build being repaired
    if router_failures >= 2 or wants_builder(task, risk, needs_build):
        return "deepagents"
    return "router"


def select_worker(name: str, *, client=None, model: str = "",
                  base_url: str = "", api_key: str = "",
                  **kwargs):
    """Construct a worker by name. 'router' needs a client+model; 'deepagents'
    needs base_url+api_key+model and the optional extra (raises a clear
    RuntimeError if it is not installed)."""
    name = (name or "router").lower()
    if name == "router":
        if client is None:
            raise ValueError("router worker requires a client")
        return RouterWorker(client=client, model=model, **kwargs)
    if name == "deepagents":
        if not deepagents_available():
            raise RuntimeError(_INSTALL_HINT)
        from .deepagents_worker import DeepAgentsWorker
        return DeepAgentsWorker(base_url=base_url, api_key=api_key,
                                model=model, **kwargs)
    raise ValueError(f"unknown worker {name!r} (router|deepagents)")
