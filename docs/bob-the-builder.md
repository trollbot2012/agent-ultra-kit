# bob — the 10-step enforced build pipeline

`bob` composes the kit's pieces — ultracode fan-out, the adversarial panel,
the command broker's risk tiers, and signed receipts — into one enforced
build loop. The doctrine:

```
real execution -> receipt -> gate validates the chain -> commit/report
```

Ten steps, six of them gated. You cannot claim a step ran: the gate
re-derives what it can and cross-checks the rest, and every receipt is
hash-chained and HMAC-signed, so skipping, faking, editing, re-ordering, or
hand-authoring a step blocks the commit.

## The pipeline

| # | step | receipt | how it's enforced |
|---|------|---------|-------------------|
| 1 | SPEC | agent | one-paragraph spec recorded (judgment step) |
| 2 | RED | **system** | written by the pytest runner from actual output; needs ≥1 FAILED, 0 collection ERRORs |
| 3 | GREEN | **system** | all tests pass; every RED test id must appear in the GREEN run |
| 4 | REFACTOR | agent | judgment step; edited files re-pin their hashes |
| 5 | CODE-QUALITY | agent | judgment step; checklist recorded |
| 6 | SECURITY-FANOUT | **engine** | cross-checked: points at an ultracode run whose own checksummed receipt must re-verify, with ≥1 real agent call |
| 7 | WORKFLOW | **engine** | same cross-check + non-empty review dimensions |
| 8 | ULTRA | **engine** | the panel writes its execution receipt from real model calls — a self-review is not a panel |
| 9 | QUIZ | agent | `passed` requires a captured operator response; otherwise the honest outcome is `skipped` |
| 10 | COMMIT | gate | `gate-pass.json` written by the gate itself; ACTIVE marker released |

`writer` records who produced each receipt's evidence. The gate trusts
nothing it cannot re-derive (live pytest re-run, file staleness hashes) or
cross-check (ultracode receipts, the panel execution receipt).

## Quick start (no API key)

```bash
pip install pytest      # bob's only runtime dependency (RED/GREEN run it)
agent-ultra bob run "add a slugify helper" --mock
```

Mock mode swaps only the MODEL CONTENT (spec text, tests, implementation)
for a bundled sample task. Everything else is real: pytest runs RED and
GREEN as subprocesses, ultracode fans out on the mock route and writes its
checksummed receipts, the panel executes and writes its execution receipt,
and the gate validates the whole chain (including a live pytest re-run).
`agent-ultra build run ...` is the same command under its alias.

On a real endpoint (after `agent-ultra init` + key env var), drop `--mock`:
the spec, tests, and implementation come from your configured model. If the
model's tests don't genuinely fail at RED or its implementation doesn't
genuinely pass at GREEN, the run ends **blocked** — the pipeline never
pretends.

## The receipt chain

Receipts live in `<workspace>/.agent-ultra/bob/runs/<run_id>/`, one JSON per
step, each carrying:

- `receipt_sha256` — integrity: the body was not edited after writing;
- `receipt_hmac` — authenticity: written by something holding the
  per-install key (created at first use under the agent-ultra home, never
  inside the workspace);
- `prev_sha256` — the sha256 of the receipt before it. Editing or replacing
  ANY earlier receipt breaks every later one;
- `files` — sha256 of every file the step covered. A covered file that
  changes afterward is **stale** and blocks until the owning step re-runs;
- `mock` — set on mock-route evidence. The gate only accepts mock receipts
  with `--allow-mock` (demo mode), never by default.

## The gate (commit/report chokepoint)

```bash
agent-ultra bob gate                      # validate the active run's chain
agent-ultra bob gate --mark-pass          # validate + record the pass, keep ACTIVE
agent-ultra bob seal                      # release ACTIVE (needs a recorded pass)
agent-ultra bob gate --complete-on-pass   # mark + seal in one step (no commit involved)
agent-ultra bob status                    # which steps have receipts
```

The gate fail-closes: a check it cannot perform (missing receipt, unreadable
artifact, unverifiable chain) is a failure, not a warning. On block, the
errors name exactly which step to redo. Staged files that appear in no
receipt also block (`*.md`, `.agent-ultra/*`, and `.gitignore` are exempt).

### Run scope: a run is FOR something

Every run declares its target scope at start — the files/dirs/globs the run
is for. `run_bob` declares the generated test + implementation files
automatically; from Python pass `scope=[...]` to `BobRun.start`. The gate
then rejects any staged file **outside** the declared scope, so a run opened
for module A cannot smuggle in edits to module B:

```
out-of-scope: staged file other/thing.py is outside the run's declared scope
```

Rules:

- **Scope is mandatory.** `BobRun.start` refuses a run with no scope, and a
  scope stripped from `run.json` after the fact unbinds nothing — a no-scope
  run authorizes NO staged files (fail closed).
- **Expansion is explicit, validated, and logged.** If the work legitimately
  grows, `agent-ultra bob scope-add <paths...>` appends to the scope and
  records the expansion in `scope-log.jsonl`. Nothing else widens a run.
- **Infrastructure is never in scope.** `.git/hooks` and `.agent-ultra`
  paths are rejected at start and at scope-add, always — a run cannot
  declare authority over the machinery that enforces it.
- **A run cannot be dropped by starting another.** `start` refuses while
  an unproven run is ACTIVE, and a marker naming a deleted run directory is
  a tampering sentinel (the gate fails closed), not "no run". The only
  sanctioned drop is `agent-ultra bob abandon --operator-abandon
  [--reason]` — durably recorded in `.agent-ultra/bob/abandoned.jsonl`.
  The command broker classifies all `--operator-*` escape flags as
  DANGEROUS, so a broker-mediated agent shell cannot auto-run them.
- **A cloned fan-out is one review.** If every completed agent of a
  step-6/7 ultracode run produced identical output, the gate blocks — the
  per-agent `output_sha256` in the run receipt exposes the clone.
- **The operator escape is not a CLI flag.** `BobRun.start(...,
  operator_unbounded=True)` opens a run with no scope binding; it is a
  Python-API parameter on purpose (no CLI verb exposes it, so an agent
  driving the CLI cannot reach it), it is announced loudly on stderr, and
  it is durably recorded in `scope-log.jsonl`.

### Commit lifecycle: mark at pre-commit, seal at post-commit

Sealing inside pre-commit is a deadlock: the pass would release ACTIVE
*before* the commit lands, so any require-run policy then blocks the very
commit the gate just validated (and if the commit fails, the run is wrongly
closed). The split lifecycle avoids it:

1. **pre-commit** — `bob gate --hook --mark-pass`: validates the chain; on
   pass it writes `gate-pass.json` but **keeps ACTIVE**.
2. the commit lands.
3. **post-commit** — `bob seal --if-passed`: releases ACTIVE and stamps the
   landed commit hash into `gate-pass.json`. `seal` refuses when no gate
   pass is recorded — a run can never be sealed unproven.

Install all three hooks (pre-commit, pre-merge-commit — git fires that one
for merge commits — and post-commit) in one command:

```bash
agent-ultra bob hook-install
```

With no active run the gate exits 0 immediately (non-interference). To make
the pipeline mandatory — every commit needs an active, proven run — export
`AGENT_ULTRA_REQUIRE_RUN=1` in the agent's environment: hook-mode gates then
block commits that have no run at all.

`--complete-on-pass` remains for closures that happen *outside* a commit
(the mock demo's report path, an operator retiring a run manually).

From Python, `assert_bob_done(workspace)` raises `BobProofError` when a run
is active and unproven — wire it in front of your agent's completion claims:

```python
from agent_ultra.bob import assert_bob_done, BobProofError
assert_bob_done(".")     # raises unless the receipt chain validates
```

`assert_bob_done` passes `staged_files=[]` (at claim time nothing is staged),
so it validates the receipt chain but does **not** run the staged-file
coverage check — that lives in the `agent-ultra bob gate` pre-commit path,
which reads git's staged list. Install both: `assert_bob_done` guards
completion claims, the pre-commit hook guards the commit itself and catches
any staged file that no receipt covers.

## The surgical lane (inert edits only)

Typo-class doc/config edits do not need RED/GREEN and a five-agent sweep.
The surgical lane is the sanctioned lightweight tier — and it NARROWS Bob,
it never replaces it:

```bash
# edit the files first, then:
agent-ultra bob surgical "fix readme wording" --files README.md
agent-ultra bob surgical "fix readme wording" --files README.md --mock   # offline demo
```

The lane runs a real two-lens review fan-out (accuracy + safety, ultracode-
backed and fingerprint-pinned exactly like steps 6/7), an operator quiz
(`passed` requires your captured answer; an unanswered quiz is honestly
`skipped`), and its own gate.

Entry criteria are objective and validated at start — anything else raises
and the answer is the full pipeline:

- **Inert types only.** Prose (`.md`/`.rst`/`.txt`/...) and known-inert
  dotfiles auto-qualify. Structured config (yaml/json/toml/ini/cfg) is
  dual-use: it needs the operator's explicit
  `AGENT_ULTRA_SURGICAL_ALLOW_CONFIG=1` AND must clear the denylist.
- **The denylist is executable-by-context files** — CI workflows, git-hook
  managers, task runners, package manifests, tool configs (matched by name,
  path, and shape) — regardless of extension. A doc ABOUT Dockerfiles is a
  doc; a `Dockerfile` is not.
- **Declared files are the exact scope.** The gate rejects any staged file
  that was not declared, is not inert, or is denied; `scope-add` on a
  surgical run refuses risky paths.
- **A small diff budget** (100 lines / 256 KiB, binary edits rejected),
  fail-closed on any git error.

**Mode downgrade cannot launder code (C1).** `run.json`'s mode field is a
REQUEST the gate verifies, never a trusted assertion: `bob gate` routes to
the surgical gate ONLY when the actual staged set independently qualifies
(all inert, none denied, all declared). Flip a run's mode to "surgical"
with code staged and you get the FULL gate — which demands the full receipt
chain that run never produced. A full-mode run never auto-downgrades
either, even when only inert files are staged: surgical is opt-in at start.

## Trust boundary (read this)

bob resists the failure modes an agent actually falls into: **skipping** the
expensive steps, **claiming** a step ran without running it, **borrowing** a
prior run's fan-out or panel, and **editing** a receipt after the fact. It
enforces those by re-deriving what it can (a live pytest re-run) and binding
every cross-checked artifact — the ultracode run's receipt bytes and the
panel receipt's task/artifact hashes — into an HMAC-signed, hash-chained
receipt. Change any pinned artifact and the signed chain breaks.

What bob does **not** claim: to stop a fully adversarial process that has
arbitrary code execution *and* the per-install signing key. The key lives at
`<agent-ultra home>/bob.key`, readable by the same process the gate runs in,
so an attacker willing to genuinely fabricate every artifact end-to-end (run
the real fan-out over decoy code, forge matching signed receipts) can pass.
The private reference implementation closes this by writing the delegation
and clarify logs **host-side**, outside the agent's reach; the public port
has no host middleware, so its guarantee is "honest work is cheap, faking it
convincingly is not," not "unforgeable." Treat the gate as a strong
tripwire against lazy or accidental skipping, not a cryptographic proof
against a determined local adversary.

## What the proofs look like

`agent-ultra doctor` and `agent-ultra demo` both run the pipeline offline
and then attack it three ways; all three must block:

1. **skipped step** — a gated receipt is removed → missing receipt + broken
   chain;
2. **fabricated fan-out** — a re-signed step-6 receipt names an invented
   ultracode run → no run receipt on disk backs the claim;
3. **doctored panel receipt** — one field changed in the panel's execution
   receipt → its checksum no longer matches.

The offline test suite (`pytest -q`) additionally proves: unsigned receipts
are forgeries, a wrong key fails closed, re-ordered/removed receipts break
linkage, `passed` quiz outcomes need an operator response, stale files
block, and the live re-run catches a state that fails its own tests.

## Troubleshooting

| symptom | fix |
|---------|-----|
| `gate BLOCKED ... receipt missing` | That step never ran (or its receipt was deleted). Re-run the step; the chain rebuilds from there. |
| `stale: <file> changed after its last covering receipt` | Re-run the step that owns the file (usually GREEN) so hashes re-pin. |
| `authenticity FAIL` on every receipt | The per-install key changed (e.g. new `AGENT_ULTRA_HOME`). Finish runs with the key that started them. |
| `receipt is marked mock` | You gated a demo run without `--allow-mock`. Mock evidence never satisfies a real gate. |
| blocked at RED on a real route | The model's tests didn't fail cleanly (or errored on import). Inspect the run dir's `step02_red.json` `output_tail`. |
