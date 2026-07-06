# Security policy

agent-ultra-kit executes model-authored commands on a host. That capability is
made safe by defaults (see [docs/security.md](docs/security.md)); please help
keep it that way.

## Reporting a vulnerability

**Do not open a public issue for an exploitable vulnerability.**

Report it privately through GitHub's **Report a vulnerability** button under
the repository's **Security** tab (this opens a private advisory). Include a
minimal reproduction and the version
(`python -c "import agent_ultra; print(agent_ultra.__version__)"`).

Examples of what to report privately:

- a way to make a **DANGEROUS** command auto-run without an approver or sandbox
- a secret shape that slips past `redact_secrets()` into a ledger or artifact
- a classifier bypass that mislabels a destructive command as SAFE/ELEVATED
- any path that executes untrusted model output without going through the broker

Non-sensitive hardening ideas, safer-default suggestions, and docs fixes can go
in a public issue via the **Security concern** template.

## Scope

The command broker is a **risk router**, not a sandbox. "A crafted command runs
in the broker's default classifier" is expected for genuinely untrusted input —
that is what the Docker sandbox adapter is for. A finding is in scope when it
defeats a documented guarantee: the deny-by-default gate, secret redaction, the
proof-gate ship check, or the "untrusted model output never executes unbroKered"
contract.

## Supported versions

Pre-1.0: only the latest release (and `main`) receive fixes.
