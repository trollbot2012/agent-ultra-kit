# Changelog

All notable changes to agent-ultra-kit are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **bob commit-lifecycle deadlock (mark/seal split).** Sealing the run inside
  a pre-commit gate released the ACTIVE marker *before* the commit landed, so
  a require-run policy blocked the very commit the gate had just validated
  (and a failed commit left the run wrongly closed). The gate now splits the
  lifecycle: `bob gate --mark-pass` (pre-commit) validates and records
  `gate-pass.json` but keeps ACTIVE; the new `bob seal` (post-commit)
  releases ACTIVE only when a gate pass is recorded, stamping the landed
  commit hash. New `bob hook-install` writes all three hooks (pre-commit,
  pre-merge-commit, post-commit); `AGENT_ULTRA_REQUIRE_RUN=1` makes the
  pipeline mandatory for hook-level commits. `--complete-on-pass` remains
  for closures outside a commit. `run_bob(..., seal_on_pass=False)` selects
  the git-workflow lifecycle from Python. Regression test drives the full
  cycle through real git: proven commit lands, post-commit seals, an
  unproven follow-up commit is blocked. **If you had wired `bob gate
  --complete-on-pass` into a pre-commit hook, replace it: run
  `agent-ultra bob hook-install` to regenerate the hooks.**

### Added
- **bob run lifecycle + fan-out integrity hardening.** A run can no longer
  be dropped quietly, and fan-out proof got stricter:
  - *No silent orphaning.* `BobRun.start(force=True)` (CLI:
    `bob run --operator-force`, renamed from `--force`) now leaves a
    durable abandonment record instead of silently superseding the
    unproven run. New `bob abandon --operator-abandon [--reason]` releases
    an unproven run explicitly — refused without the flag — logging to
    `.agent-ultra/bob/abandoned.jsonl` plus `abandoned.json` in the run
    dir, and it announces loudly on stderr.
  - *Operator escapes are agent-unreachable.* The command broker
    classifies `--operator-abandon` / `--operator-force` /
    `--operator-unbounded` as DANGEROUS, so a broker-mediated agent shell
    cannot auto-run them.
  - *Cloned fan-out rejected.* A step-6/7 ultracode run whose completed
    agents all produced IDENTICAL output (per-agent `output_sha256` in the
    run receipt) is one review wearing N hats — the gate now blocks it.
    Demo mode (`--allow-mock`) is exempt: the offline mock answers every
    agent identically by design, and mock evidence is already only
    acceptable there.
  - **Breaking:** `bob run --force` is now `bob run --operator-force`.

- **bob run scope — a run is bound to what it declared.** Every bob run now
  declares its target scope at start (`BobRun.start(..., scope=[...])`;
  `run_bob` declares the generated test+impl files automatically) and the
  gate rejects staged files outside it, so a run opened for module A cannot
  smuggle in edits to module B. Scope is mandatory and fail-closed: a
  no-scope start refuses, and a scope stripped from `run.json` afterwards
  authorizes nothing. Expansion goes only through the validated, logged
  mechanism (`agent-ultra bob scope-add`, recorded in `scope-log.jsonl`).
  `.git/hooks` and `.agent-ultra` paths are rejected as scope entries
  always — a run cannot declare authority over its own enforcement. The
  only unbounded form is `operator_unbounded=True`, a Python-API parameter
  deliberately absent from the CLI (an agent driving the CLI cannot reach
  it), loudly announced and durably logged. **Breaking:** `BobRun.start`
  now requires `scope=`; `run_bob` callers are unaffected.

- **bob** (`agent_ultra.bob`) — the 10-step enforced build pipeline
  (SPEC → RED → GREEN → REFACTOR → CODE-QUALITY → SECURITY-FANOUT → WORKFLOW
  → ULTRA → QUIZ → COMMIT) composing ultracode fan-out, the adversarial
  panel, the broker's risk tiers, and signed receipts into one loop. Each
  gated step leaves a hash-chained (`prev_sha256`), HMAC-signed step receipt
  written from real execution: RED/GREEN receipts come from the pytest
  runner's actual output (`writer="system"`); the two fan-out steps are
  cross-checked against ultracode's own checksummed run receipts; ULTRA is
  proven by the panel execution receipt (a self-review is not a panel); a
  `passed` quiz needs a captured operator response. The commit/report gate
  re-runs pytest live, re-hashes covered files (staleness), demands coverage
  for staged files, and fail-closes on anything it cannot verify — a
  skipped, fabricated, edited, re-ordered, or hand-authored step blocks.
  Mock mode (`--mock`, no API key) swaps only the model content for a
  bundled sample task; pytest, ultracode, the panel, and the gate all really
  execute. CLI: `agent-ultra bob run|gate|status` (alias: `agent-ultra
  build`); Python: `run_bob` / `gate_check` / `assert_bob_done`. `doctor`
  and `demo` prove the pass AND three blocked-fraud scenarios; see
  [docs/bob-the-builder.md](docs/bob-the-builder.md). Each cross-checked
  artifact (the ultracode run receipt's file bytes, the panel receipt's
  task/artifact hashes) is bound into the signed chain, and model-authored
  file paths are validated against workspace escape; the docs state the
  trust boundary honestly (no host middleware, unlike the private reference).
  29 offline tests.
- **ultracode** (`agent_ultra.ultracode`) — deterministic multi-agent workflow
  engine. Workflows are plain Python modules (`META` + `async def run(wf)`) that
  fan work across bounded agents via `wf.agent` (one model call, optional
  JSON-Schema validation with feedback retries), `wf.parallel` (barrier) and
  `wf.pipeline` (no barrier), under hard `wf.budget` call/token ceilings, with
  broker-gated `wf.run_check`. Every run writes a typed event stream
  (`events.jsonl`), a replayable journal, per-agent artifacts, and a checksummed
  receipt (`verify_receipt`). **Resume** replays completed calls from the
  journal and spends budget only on new ones (0 calls when unchanged). A
  terminal-safe status card renders from the journal (plain ASCII unless stdout
  is a proven interactive UTF terminal; never crashes the run). CLI:
  `agent-ultra ultracode run|list|status|resume`, with `--mock` for a fully
  offline, keyless route. Bundled examples: `smoke` and `review`
  (finders -> skeptic votes -> synthesis). `doctor` and `demo` now cover it; see
  [docs/ultracode.md](docs/ultracode.md). 14 offline tests.
- **Receipts bus** (`agent_ultra.receipts_bus`). A unified, authenticated
  receipt index over SQLite (WAL, default auto-checkpoint). Each receipt carries
  two hashes over its canonical body: `receipt_sha256` (integrity) and a keyed
  `receipt_hmac` (authenticity) — kept **separate**, so a hand-authored envelope
  with a valid sha256 but no HMAC is `authentic=False` and rejected by
  enforce-mode consumers. `kind`/`actor`/`verdict` are closed enums validated at
  write time. A failed read raises `BusUnavailable` (never an empty list).
  `resolve` + `binds` implement the claim-binding rule (cited / verify-command
  with scope+freshness / task_ref; command-, manual-, repair-, route_health-kind
  never complete; a verifier candidate binds only on `claim_sha256` match; weak
  evidence yields a distinct `ledger-weak` clause). `append_audit` /
  `verify_audit` maintain a hash-chained `gate_audit`. New CLI:
  `agent-ultra receipts list|show|verify|why|attest` (`attest` is
  interactive-only and writes `kind=manual`, which never satisfies completion).
- **Verifier** (`agent_ultra.verifier`). Refute-first claim verification with an
  injected check-runner and judge (no network, no host commands by default).
  Engine re-check wins when a verify command is declared (exit 0 confirms,
  non-zero refutes); otherwise a refute-first `judge` runs, defaulting to
  refuted on insufficient evidence; with neither channel, refuted. Emits a
  `kind=verifier` receipt stamped with `claim_sha256`; a refuted verdict maps to
  a non-acceptable receipt verdict so it can never satisfy a gate. `Verifier`
  wraps it with a configurable escalation budget (per-session/per-window caps +
  dedup on identical `claim_sha256`).
- **Leak gate** (`agent_ultra.leakgate`, `agent-ultra-leakgate`). Scans the
  whole tree (and a commit message / PR body) against a base64 denylist decoded
  at runtime, so the denylist file never contains plaintext. Case-insensitive
  except a whole-word case-sensitive collision token; path separators
  normalised; no file whitelist. Wired into CI as a dedicated job.

### Changed
- Renamed the optional memory adapter to `agent_ultra.adapters.external_memory`
  (`ExternalMemoryHooks`) and made the worker-layer leakage test consume the
  shared base64 denylist, so no host-specific identifier remains in the tree.

- **Structural PANEL enforcement** (`agent_ultra.panel_receipt`). A phase
  labelled PANEL is no longer proof — only a valid panel execution receipt with
  real executed lenses satisfies it. The loop writes
  `panel_execution_receipt.json` from the REAL `PanelReport` (`model_calls`,
  `lenses`, per-finding origins) with a mandatory `receipt_sha256` checksum,
  validates it before REPORT (`UltraReport.panel_enforced`), and
  `gate_report(run_dir)` / `agent-ultra panel-gate <run_dir>` blocks REPORT
  unless real panel-agent calls happened. A self-review (0 model calls) yields
  `lens_count_executed == 0` and fails with
  `PANEL phase completed with 0 agent calls — self-review is not a panel.`
  Stdlib, additive; does not weaken the existing proof gates.
- **Hybrid worker layer** (`agent_ultra.workers`). Ultra stays the supervisor
  (build → test → panel → classify → fix → re-test → re-panel → proof gate);
  a *worker* fills only the builder/fixer slots.
  - `RouterWorker` — the default. Stdlib, one model call per fix, returns an
    advisory single-file edit the loop applies with automatic rollback.
  - `DeepAgentsWorker` — **optional** multi-step builder/fixer wrapping
    LangChain Deep Agents, for from-scratch multi-file work. Guarded import:
    never loaded unless selected; requires the `deepagents` extra.
  - `select.resolve_worker_choice` auto policy: router for one-file/finding
    repairs, Deep Agents for builds and escalated repairs.
  - Both workers normalize to one `WorkerResult`
    (`{worker, status, summary, files_changed, commands_run, proof, error}`),
    so the loop is worker-agnostic. Workers never bypass the gates — edits
    still flow through tests, the panel, and the proof artifacts.
- Optional extra `agent-ultra-kit[deepagents]` (deepagents + langchain +
  langchain-openai + langgraph). Core install stays **zero-dependency**.
- `agent-ultra ultra --worker/--builder/--fixer/--build` for worker selection;
  `agent-ultra doctor --deepagents` reports optional-extra availability
  (absence is a PASS — it is never required).

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
- PyPI release workflow (`.github/workflows/release.yml`) using **Trusted
  Publishing (OIDC)** — no stored token. Fires only on `v*` tags, gates on
  tests + tag/version match + build + `twine check`, publishes only if all
  pass, and supports a build-only / TestPyPI dry run via manual dispatch.
  Not yet wired to a live PyPI project (setup pending). See
  [docs/releasing.md](docs/releasing.md).

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
  LiteLLM, Docker sandbox, external-memory, Hermes-style, Ktisis-style.
- **Install layer**: `agent-ultra doctor` / `init` / `demo`, one-command
  installers for PowerShell / bash / CMD, `config.example.yaml`, `.env.example`,
  `INSTALL.md` with an AI-agent handoff prompt, and a troubleshooting guide.
- 63 offline tests; CI on Ubuntu + Windows across Python 3.10 / 3.12 / 3.13.

[Unreleased]: https://github.com/trollbot2012/agent-ultra-kit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/trollbot2012/agent-ultra-kit/releases/tag/v0.1.0
