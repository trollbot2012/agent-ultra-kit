"""Basic panel example — fully offline with the mock route.

    python examples/basic_panel/run.py

Swap `demo_panel_client()` for an `OpenAIChatClient(base_url=..., api_key_env=...)`
and a real model name to run against a live endpoint.
"""

from agent_ultra.routes.pool import RoutePool
from agent_ultra.routes.mock import demo_panel_client
from agent_ultra.panel.engine import PanelEngine

SOURCE = '''
def authenticate(request):
    """Return True if the request is authenticated."""
    return "token" in request.headers   # <-- only checks key presence
'''


def main():
    pool = RoutePool(["mock-a"], client=demo_panel_client())
    engine = PanelEngine(pool)
    report = engine.run(
        "Is this authentication function safe for production?",
        size="small", lenses=["security", "correctness", "failure-modes"],
        context=SOURCE)

    print(f"Decision: {report.decision}\n")
    print(f"Verdicts: {report.verdict_counts()}\n")
    for f in report.accepted:
        print(f"  [{f.verdict}/{f.severity}] ({f.lens}) {f.claim}")
        if f.reasoning:
            print(f"      judge: {f.reasoning}")
    if report.proof_gates:
        print("\nProof gates (read-only, safe to run):")
        for g in report.proof_gates:
            print(f"  $ {g}")


if __name__ == "__main__":
    main()
