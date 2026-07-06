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

## Install (pick one)

**One command — Windows PowerShell:**

```powershell
irm https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.ps1 | iex
```

**One command — Linux/macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.sh | bash
```

**pip / uv:**

```bash
pip install git+https://github.com/trollbot2012/agent-ultra-kit.git
```

**Hand it to your AI agent:** paste the install prompt from
[INSTALL.md](INSTALL.md#3-ai-agent-handoff-install) into Claude Code / Cursor /
your own agent and let it install, configure, and verify for you.

Every path ends the same way — prove it works, no API key required:

```bash
agent-ultra doctor    # 8-point health check (offline)
agent-ultra demo      # full panel + broker + ULTRA loop, offline, ~2s
```

## 60 seconds, no API key

```bash
agent-ultra --mock panel "Is this auth service safe?" --lenses security,correctness
```

```
DECISION: Fix the empty-token acceptance before shipping; ...

verdicts: {'real_now': 2, 'real_later': 0, 'theoretical': 0, 'wrong': 0, 'unjudged': 0}
  [real_now/critical] (security) Empty or whitespace-only token is accepted...
  [real_now/critical] (correctness) Return value is unvalidated...

proof gates:
  $ grep -n 'token' service.py
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
