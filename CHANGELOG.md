# Changelog

All notable changes to agent-ultra-kit are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `install.cmd` is now stored with verbatim CRLF line endings (via
  `.gitattributes` `-text`) so `curl | cmd` receives a parseable batch file;
  the previous LF-only blob made CMD misparse every line and silently skip the
  real install. Verified end to end in Windows CMD.
- Replaced a non-ASCII character in `install.cmd` for maximum code-page safety.

### Added
- `.gitattributes` pinning line endings per file type (`*.cmd`/`*.bat` verbatim
  CRLF, `*.sh`/`*.ps1`/`*.py` LF) so installers stay valid on every platform.
- Real terminal transcript and an "AI-agent handoff" section near the top of
  the README.
- Issue templates: install problem, adapter request, bug report, security
  concern.

### Verified
- `install.sh` exercised in a clean `python:3.12-slim` Linux container from the
  public raw URL (doctor 8/1/0, demo passed, uninstall clean).
- `install.cmd` exercised via real Windows CMD (doctor 9/0/0, demo passed).

## [0.1.0] — 2026-07-06

First public release. Portable, stdlib-only core with thin optional adapters.

### Added
- **Adversarial panel** (`agent_ultra.panel`): critic lenses → steelman →
  judge cross-exam → synthesis. Verdicts `real_now` / `real_later` /
  `theoretical` / `wrong`; a failed judge call is marked `unjudged`, never
  silently downgraded. Panel agents are roles, not models — one healthy route
  runs a whole panel; `mixed` mode spreads across routes and collapses to
  `single` when only one is healthy. Proof gates derive only from accepted
  findings' read-only checks; destructive checks are split out.
- **ULTRA loop** (`agent_ultra.ultra_loop`): build → test → panel → classify →
  fix → re-test → re-panel → ship gate. Red tests stop before the panel;
  low-context panels are refused; only proof-gated work ships.
- **Command broker** (`agent_ultra.broker`): SAFE / ELEVATED / DANGEROUS
  classification with a JSONL ledger. Deny-by-default for a DANGEROUS command
  with no approval path; opt into an approver, a sandbox, or the explicit
  `allow_dangerous_without_approval` override (which still never auto-runs).
- **Proof gates** (`agent_ultra.proof`): "done" requires recorded evidence;
  `assert_shippable()` raises on unsupported completion claims.
- **Evidence reader** (`agent_ultra.evidence`): bounded source gathering with
  secret redaction and low-context detection.
- **Route pool** (`agent_ultra.routes`): health-probed OpenAI-compatible model
  routing with a degradation ladder; a stdlib `OpenAIChatClient` and a
  deterministic offline `MockChatClient`.
- **Artifacts** (`agent_ultra.artifacts`): uniform JSON + Markdown run records
  and JSONL ledgers, secret-redacted on write.
- **Memory hooks** (`agent_ultra.memory`): five generic events, all no-ops by
  default; no memory system required.
- **Adapters** (all optional, none imported by the core): generic CLI,
  LiteLLM, Docker sandbox, Mneme-style memory, Hermes-style, Ktisis-style.
- **Install layer**: `agent-ultra doctor` / `init` / `demo`, one-command
  installers for PowerShell / bash / CMD, `config.example.yaml`, `.env.example`,
  `INSTALL.md` with an AI-agent handoff prompt, and a troubleshooting guide.
- 63 offline tests; CI on Ubuntu + Windows across Python 3.10 / 3.12 / 3.13.

[Unreleased]: https://github.com/trollbot2012/agent-ultra-kit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/trollbot2012/agent-ultra-kit/releases/tag/v0.1.0
