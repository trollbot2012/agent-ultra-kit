"""Ktisis adapter example.

A Ktisis-style build agent wires its coding agent into the ULTRA loop through
two seams — a builder and a fixer — while the panel, broker, tests, proof
gates, and artifacts come from the portable core.

No network by default: with no KTISIS_ROUTES set it prints the intended
wiring. Set the env vars + a running router to go live.
"""

import os


def fake_builder(workspace, task):
    """Real version: drive your coding agent to implement `task` in workspace."""
    return f"(agent would implement: {task})"


def fake_fixer(fix_task, workspace):
    """Real version: drive your coding agent to fix one finding. Return True
    if the fix was applied."""
    print(f"  fixer: would resolve [{fix_task.lens}] {fix_task.claim[:70]}")
    return True


def main():
    if not os.environ.get("KTISIS_ROUTES"):
        print("KTISIS_ROUTES not set — showing intended wiring only.\n")
        print("  builder(workspace, task) -> drives the coding agent")
        print("  fixer(fix_task, workspace) -> bool, applies one fix")
        print("  panel + broker + tests + proof gates come from the core")
        print("\nSet KTISIS_ROUTER_URL / KTISIS_ROUTES / KTISIS_HOME to go live.")
        return

    from agent_ultra.adapters.ktisis import ktisis_ultra_loop
    loop = ktisis_ultra_loop(".", builder=fake_builder, fixer=fake_fixer)
    report = loop.run("add a rate limiter", risk="high", test_cmd="pytest -q")
    print(f"Shipped: {report.shipped} — {report.ship_reason}")


if __name__ == "__main__":
    main()
