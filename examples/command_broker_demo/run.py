"""Command broker demo — classify and route commands by risk tier.

    python examples/command_broker_demo/run.py

Shows: SAFE reads auto-run, ELEVATED dev commands auto-run in owner mode,
DANGEROUS commands DENY when there is no approval path (the safe default),
and how an approver or the override changes that.
"""

import tempfile
from pathlib import Path

from agent_ultra.broker.broker import (
    CommandBroker, TRUSTED_OWNER_TIERS, CRITIC_TIERS, classify,
)

COMMANDS = [
    "echo hello world",       # SAFE  — pure read, auto-runs
    "git status",             # SAFE  — read-only git query
    "pytest -q",              # ELEV. — arbitrary code, owner auto-runs
    "pip install requests",   # ELEV. — installs
    "rm -rf build",           # DANG. — deletes files
    "git push --force",       # DANG. — irreversible history
    "cat .env",               # DANG. — touches secrets
]


def show(title, broker):
    print(f"\n=== {title} ===")
    for cmd in COMMANDS:
        res = broker.run(cmd)
        ran = "RAN" if res.ran else "not run"
        print(f"  {res.risk_tier.upper():9} {res.status:18} {ran:8} | {cmd}")


def main():
    ledger = Path(tempfile.mkdtemp()) / "broker.jsonl"

    # Trusted-owner: SAFE+ELEVATED auto-run; DANGEROUS with no approver DENIES.
    show("TRUSTED OWNER (no approval path)",
         CommandBroker(ledger_path=ledger, auto_run_tiers=TRUSTED_OWNER_TIERS))

    # Critic mode: only SAFE auto-runs; everything else parks for approval.
    show("CRITIC MODE",
         CommandBroker(ledger_path=ledger, auto_run_tiers=CRITIC_TIERS))

    # With an approver, dangerous commands can be gated interactively.
    def approve_reads_only(res):
        return classify(res.command)[0] != "dangerous"
    show("TRUSTED OWNER + approver (rejects dangerous)",
         CommandBroker(ledger_path=ledger, auto_run_tiers=TRUSTED_OWNER_TIERS,
                       approver=approve_reads_only))

    print(f"\nLedger written to: {ledger}")


if __name__ == "__main__":
    main()
