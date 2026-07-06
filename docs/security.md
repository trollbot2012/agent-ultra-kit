# Security

agent-ultra-kit executes model-authored commands on a host. That is a
deliberate capability, made safe by defaults — not something to bolt on later.

## Threat model

- **Untrusted input:** model output (critic-proposed checks, fix commands) is
  treated as untrusted. It is classified and gated, never executed blindly.
- **In scope:** preventing accidental destructive execution, keeping secrets
  out of logs/artifacts, and never claiming completion without evidence.
- **Out of scope:** the broker is a *risk router*, not a security sandbox. A
  determined adversary with a crafted command is the sandbox's job (see the
  Docker adapter). Use it for genuinely untrusted or destructive work.

## Safe defaults

- **DANGEROUS with no approval path is denied**, not silently parked. You must
  opt into an approver, a sandbox, or the explicit
  `allow_dangerous_without_approval` override (which still never auto-runs).
- **Critic mode auto-runs SAFE only.** Model-*proposed* checks that read are
  fine; anything with side effects is parked.
- **Unknown commands are ELEVATED**, so they are gated in critic mode.
- **Every command is ledgered** — command, tier, status, output, timestamp.

## Secret hygiene

- `redact_secrets()` scrubs common secret shapes (OpenAI/GitHub/AWS/Slack
  tokens, JWTs, bearer headers, `key = value` pairs, PEM private-key blocks)
  from gathered evidence, broker output, and artifacts before they are written.
- API keys are read from **named environment variables**, never passed as raw
  config the kit would persist.
- The ledger stores command lines **verbatim** for audit exactness — do not
  put secrets on command lines.

## What this kit does NOT ship

No API keys, no operator paths, no private profiles, no runtime-specific
assumptions in the core. Adapters read all endpoints/keys/paths from the
environment.

## Reporting

Found a way to make a DANGEROUS command auto-run without an approver, or a
secret shape that slips past redaction? Open an issue (or a private report if
the repo enables it) with a minimal reproduction.
