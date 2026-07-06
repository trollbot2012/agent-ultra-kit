"""Hermes adapter example.

Shows how a Hermes-style agent (local model router + on-disk home) adopts the
kit by setting a few environment variables — no code changes to the core.

This example does NOT hit the network by default: with no HERMES_ROUTES set it
prints the wiring it WOULD use. Set the env vars and a running router to go
live.

    HERMES_ROUTER_URL=http://127.0.0.1:4000/v1 \
    HERMES_ROUTER_KEY_ENV=HERMES_ROUTER_KEY \
    HERMES_ROUTES=my-primary,my-cheap \
    HERMES_HOME=~/.hermes \
    python examples/hermes_adapter_example/run.py
"""

import os


def main():
    if not os.environ.get("HERMES_ROUTES"):
        print("HERMES_ROUTES not set — showing intended wiring only.\n")
        print("  router url : $HERMES_ROUTER_URL (default 127.0.0.1:4000/v1)")
        print("  key env    : $HERMES_ROUTER_KEY_ENV (default HERMES_ROUTER_KEY)")
        print("  routes     : $HERMES_ROUTES (comma-separated model aliases)")
        print("  home       : $HERMES_HOME (broker ledger + artifacts)")
        print("\nThen: hermes_panel().run(...) and hermes_broker().run(...)")
        return

    from agent_ultra.adapters.hermes import hermes_panel, hermes_broker
    engine = hermes_panel()
    broker = hermes_broker(critic_mode=False)  # trusted owner
    report = engine.run("Review the pending change for production safety.",
                        size="small",
                        context="(pass real source via evidence_dirs=...)")
    print(f"Decision: {report.decision}")
    print(f"Broker ledger: {broker.ledger_path}")


if __name__ == "__main__":
    main()
