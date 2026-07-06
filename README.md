# agent-ultra-kit

**Adversarial panel · ULTRA loop · command broker · proof gates — for any AI
agent runtime.**

Tests prove the *known* contract. An adversarial panel finds the *unknown*
failure modes. Proof gates decide what ships. A command broker makes host
execution powerful *and* accountable. This kit packages those patterns as a
portable core (pure stdlib, zero dependencies) with thin adapters, so you can
bolt them onto your own agent in an afternoon.

Born from a real result: a small auth service passed 16/16 tests while an
adversarial panel found 8 real production-safety issues with **zero overlap**.
Green tests alone are not enough.

## How to give this to your AI agent

The fastest way in: paste this into Claude Code, Cursor, aider, or your own
agent, and it installs, configures, and proves the kit for you.

> Install agent-ultra-kit into this project and prove it works.
> Install with `pip install git+https://github.com/trollbot2012/agent-ultra-kit.git`,
> run `agent-ultra init`, then `agent-ultra doctor` and `agent-ultra demo` and
> show me the output — the demo must end with "DEMO PASSED". Then run
> `agent-ultra panel "Is this module production-safe?" --evidence-dir ./src`
> (add `--mock` if I have no model endpoint configured). If anything fails,
> read docs/troubleshooting.md in the repo, fix it, and rerun.

The full handoff prompt (with model-route config and rollback) is in
[INSTALL.md](INSTALL.md#3-ai-agent-handoff-install).

## Install it yourself (pick one)

**One command — Windows PowerShell:**

```powershell
irm https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.ps1 | iex
```

**One command — Linux/macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.sh | bash
```

**One command — Windows CMD:**

```bat
curl -fsSL https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.cmd -o install.cmd && install.cmd
```

**pip / uv:**

```bash
pip install git+https://github.com/trollbot2012/agent-ultra-kit.git
```

## See it work (no API key)

Every install path ends by proving itself. This is a real, unedited run of the
offline demo — the deterministic mock route drives the whole loop, so it works
on any machine with zero configuration:

```console
$ agent-ultra doctor
  [PASS] python 3.12.13  — need >= 3.10
  [PASS] agent_ultra 0.1.0 importable
  [PASS] config source: (defaults + env)  — routes=gpt-4o-mini base=http://127.0.0.1:4000/v1
  [WARN] model route configured (key env OPENAI_API_KEY NOT set)  — offline mock works regardless
  [PASS] artifact dir writable: panel-runs
  [PASS] command broker: safe auto-runs, dangerous denies, ledger written
  [PASS] panel demo: 3 findings, 3 accepted, decision produced
  [PASS] ULTRA demo: build->test->panel->fix loop->proof artifacts
  [PASS] secret hygiene: redaction active, no secrets in artifacts
Result: 8 pass, 1 warn, 0 fail

$ agent-ultra demo
[ok] panel: 3 findings, 3 accepted -> Fix the empty-token acceptance before shipping...
[ok] broker: safe=passed, dangerous=denied (deny-by-default)
[ok] ultra: tests green -> panel -> 3 fix task(s) -> fix loop -> proof saved
DEMO PASSED — the full loop works on this machine.

$ agent-ultra --mock panel "Is this auth service safe?" --lenses security,correctness,failure-modes
DECISION: Fix the empty-token acceptance before shipping; schedule the None-return validation.

verdicts: {'real_now': 3, 'real_later': 0, 'theoretical': 0, 'wrong': 0, 'unjudged': 0}
  [real_now/critical] (security) Empty or whitespace-only token is accepted as authenticated
                                 because the check only tests for key presence, not value.
  [real_now/critical] (correctness) Return value is unvalidated: a None result from the lookup
                                    propagates to the caller and crashes downstream formatting.
  [real_now/critical] (failure-modes) No timeout on the outbound call; a hung dependency stalls
                                      the worker pool indefinitely.

proof gates:
  $ grep -n 'token' service.py
  $ grep -n 'return' service.py
```

Then point it at a real model (any OpenAI-compatible endpoint — OpenAI,
LiteLLM, vLLM, Ollama, LM Studio):

```bash
agent-ultra init                      # writes agent-ultra.yaml + .env.example
# edit agent-ultra.yaml: base_url, routes; put your key in the env var it names
agent-ultra doctor --live             # probe the endpoint
agent-ultra panel "Is this change production-safe?" --evidence-dir ./src
```

## What's in the box

| module | what it does |
|--------|--------------|
| **panel** | Adversarial review: parallel critic *lenses* → steelman → judge cross-exam → synthesis. Verdicts: `real_now` / `real_later` / `theoretical` / `wrong`. Panel agents are roles, not models — one healthy route runs a whole panel. |
| **ultra_loop** | build → test → panel → classify → fix → re-test → re-panel → **ship gate**. Red tests stop before the panel; low-context panels are refused; only proof-gated work ships. |
| **broker** | Every model-authored command classified SAFE / ELEVATED / DANGEROUS, ledgered, and executed only if its tier allows. DANGEROUS with no approval path is **denied by default**. |
| **proof** | "Done" requires recorded evidence. Accepted findings become gates; `assert_shippable()` raises on unsupported completion claims. |
| **evidence** | Bounded source gathering with secret redaction; low-context detection. |
| **routes** | Health-probed model routes with degradation: dead routes fall through, mixed mode collapses to single, zero routes fails loudly. |
| **artifacts** | Uniform JSON + Markdown run records and JSONL ledgers for every run. |
| **memory** | Five generic write-back hooks (`on_panel_decision`, `on_finding_accepted`, `on_command_run`, `on_task_complete`, `on_lesson_learned`). No memory system required. |

Adapters (all optional): generic CLI, LiteLLM, Docker sandbox, memory
(Mneme-style), Hermes-style and Ktisis-style runtimes.

## Use it with YOUR agent

Your agent needs exactly one thing — a way to call a model:

```python
from agent_ultra import PanelEngine, RoutePool, OpenAIChatClient

pool = RoutePool(["your-model"],
                 client=OpenAIChatClient("https://your-endpoint/v1",
                                         api_key_env="YOUR_KEY_ENV"))
report = PanelEngine(pool).run("Is this safe to ship?", evidence_dirs=["./src"])
if report.accepted:
    ...  # feed report.accepted into your agent's task queue
```

Wire the deeper loop with two callables that drive your coding agent:

```python
from agent_ultra import UltraLoop, CommandBroker, TRUSTED_OWNER_TIERS

loop = UltraLoop(".", panel=engine,
                 broker=CommandBroker(ledger_path=".ultra/broker.jsonl",
                                      auto_run_tiers=TRUSTED_OWNER_TIERS),
                 builder=my_agent_builds,     # (workspace, task) -> str
                 fixer=my_agent_fixes)        # (fix_task, workspace) -> bool
report = loop.run("add token auth", risk="high", test_cmd="pytest -q")
print(report.shipped, report.ship_reason)
```

Full guide: [docs/adapter-guide.md](docs/adapter-guide.md).

## Docs

[architecture](docs/architecture.md) · [panel](docs/panel.md) ·
[ULTRA loop](docs/ultra-loop.md) · [command broker](docs/command-broker.md) ·
[proof gates](docs/proof-gates.md) · [adapter guide](docs/adapter-guide.md) ·
[security](docs/security.md) · [troubleshooting](docs/troubleshooting.md) ·
[INSTALL](INSTALL.md)

## Security posture (short version)

Model output is untrusted input. Dangerous commands deny without an approval
path. Critic-proposed checks auto-run only if they are pure reads. Secrets are
redacted from evidence, ledgers, and artifacts. Keys live in env vars the
config only *names*. Details: [docs/security.md](docs/security.md).

## Development

```bash
git clone https://github.com/trollbot2012/agent-ultra-kit.git
cd agent-ultra-kit
pip install -e ".[dev]"
pytest -q          # 60+ tests, all offline
python examples/basic_panel/run.py
python examples/command_broker_demo/run.py
python examples/ultra_loop_demo/run.py
```

MIT license.
