# ultracode — deterministic multi-agent workflows

`ultracode` runs an operator-authored Python **workflow script** that fans work
across many bounded agents, journals every step, and proves what happened with
a checksummed receipt. It's the fan-out sibling of the panel (which debates one
question) and the ULTRA loop (which runs one task's build/test lifecycle).

The pattern it proves, end to end:

> **multi-agent fan-out → journal → resume → receipt → status**

It is stdlib at the core. The model endpoint is any OpenAI-compatible URL, and
the offline **mock route needs no API key**, so everything below runs on a
fresh machine with zero setup.

---

## Install (one command)

```bash
pip install git+https://github.com/trollbot2012/agent-ultra-kit.git
```

Then prove it works — no key required:

```bash
agent-ultra doctor        # environment + all subsystems, incl. ultracode
agent-ultra demo          # offline loop; must end with "DEMO PASSED"
```

`doctor` includes an `ultracode:` line proving fan-out → journal → resume
(cached) → valid receipt. `demo` runs the smoke workflow and resumes it.

---

## First run (offline, then real)

```bash
# offline, deterministic — works anywhere, no key
agent-ultra ultracode run smoke --mock

# see what's available
agent-ultra ultracode list --mock

# resume the run you just did — every call replays from the journal (0 calls)
agent-ultra ultracode resume <RUN_ID> --mock
```

Point it at a real model — any OpenAI-compatible endpoint (OpenAI, LiteLLM,
vLLM, Ollama, LM Studio). Routes and the key **env var name** come from
`agent-ultra.yaml` + environment; no secrets are ever written to config:

```bash
agent-ultra init                          # writes agent-ultra.yaml + .env.example
export OPENAI_API_KEY=sk-...              # or whatever env var your config names
agent-ultra ultracode run review --args '{"target": "./src"}'
```

`review` is the flagship example: parallel finders (one per lens) hunt problems
in `target`, findings are deduped, each faces independent skeptic votes, and
only findings a majority fail to refute survive to a synthesis.

---

## Writing a workflow

A workflow is a Python module with a `META` dict and an `async def run(wf)`:

```python
META = {"name": "smoke", "description": "...", "phases": ["Ping", "Chain"]}

async def run(wf):
    wf.phase("Ping")
    a, b = await wf.parallel([
        lambda: wf.agent("Reply with exactly: pong-one", label="ping-1"),
        lambda: wf.agent("Reply with exactly: pong-two", label="ping-2"),
    ])

    wf.phase("Chain")
    out = await wf.pipeline(
        ["alpha", "bravo"],
        lambda word: wf.agent(
            f"Return JSON {{'upper': '{word}' uppercased}}.",
            schema={"type": "object",
                    "properties": {"upper": {"type": "string"}},
                    "required": ["upper"]},
            label=f"upper:{word}"),
        lambda parsed, word: {"word": word, "upper": (parsed or {}).get("upper")},
    )
    return {"pings": [a, b], "chained": out}
```

Drop it in `~/.agent-ultra/ultracode/scripts/` (override the home with
`AGENT_ULTRA_HOME`) and run it by name, or pass any `.py` path directly.

### The `wf` surface

| call | what it does |
|------|--------------|
| `await wf.agent(prompt, *, schema=None, route="", label="", evidence=None, max_tokens=None)` | One bounded agent = one model call. With `schema` (a JSON-Schema subset) the reply is validated and re-asked on mismatch; returns the parsed object, or the raw text without `schema`, or `None` on failure. |
| `await wf.parallel([thunk, ...])` | Run zero-arg async thunks concurrently and **wait for all** (barrier). A thunk that raises resolves to `None`. |
| `await wf.pipeline(items, stage1, stage2, ...)` | Run each item through all stages independently — **no barrier** between stages. Stages take `(prev[, item[, index]])`, sync or async. A stage that raises drops that item to `None`. |
| `wf.budget` | Hard ceilings: `wf.budget.calls`, `.remaining_calls()`. Exceeding `--max-calls`/`--token-budget` ends the run as **HOLD** with everything journaled. |
| `wf.run_check(command, reason="")` | Execute a host command through the risk-tiered broker in CRITIC mode — only SAFE (pure-read) commands auto-run; everything else is classified, ledgered, and returned as `requires_approval`. |
| `wf.phase(title, kind="")` | Mark a phase (drives the status card). `kind` of `verification`/`synthesis`/`fan_in` tags it explicitly; otherwise the title is matched heuristically. |
| `wf.evidence(paths, ...)` | Bounded, read-only file excerpts to ground a prompt. |
| `wf.log(msg)` | Append a progress line (also written to the journal). |
| `wf.args` | Whatever you passed via `--args` (JSON or bare text). |

---

## Journal, resume, and the receipt (the source of truth)

Every run writes to `~/.agent-ultra/ultracode/runs/<run_id>/`:

- **`events.jsonl`** — typed runtime events (`agent_planned/deployed/completed/
  failed/blocked`, `phase_started`, terminal `ultracode_*`). The status card
  replays *this* — a workflow's own text output can never move it.
- **`journal.jsonl`** — one line per agent call; the replay cache for resume.
- **`agents/aNNN.json`** — the full record (prompt hash, output, route, timing)
  each event points at.
- **`receipt.json`** — a checksummed manifest: model-call count and per-agent
  prompt/output hashes. `verify_receipt()` recomputes the checksum, so a
  hand-edited receipt fails validation.
- **`report.json`** — the complete run report.

**Resume** replays completed calls from the journal and spends budget only on
new or changed ones:

```bash
agent-ultra ultracode resume <RUN_ID>          # infers the workflow from the journal
```

A resumed run reports `0` model calls when nothing changed — proof the cache,
not the model, produced the result.

---

## Status card (terminal-safe)

`agent-ultra ultracode status` prints a card per in-progress run, rebuilt from
`events.jsonl`. It's **plain ASCII by default** and only uses richer glyphs
when stdout is provably an interactive UTF-capable terminal — redirected
output, `TERM=dumb`, CI, `NO_COLOR`, or `AGENT_ULTRA_PLAIN=1` all force plain,
and any rendering error degrades rather than crashing the run.

```
ULTRACODE
Run: 20260707T201444-review-9a1c55
Phase: Verify
Agents: 6/12 complete
Failed: 0
Blocked: 0
Progress: [######......]
Status: RUNNING
```

`#` = completed, `.` = deployed/running, `!` = failed or blocked (in rich mode:
`●`, `○`, `⚠`).

---

## Using it from Python

```python
from agent_ultra.ultracode import UltracodeEngine, demo_pool, verify_receipt

eng = UltracodeEngine(demo_pool())                 # offline, keyless
report = eng.run_script(eng.resolve("smoke"))
print(report.final_state)                          # COMPLETE

# resume replays with zero model calls
resumed = eng.run_script(eng.resolve("smoke"), resume_run_id=report.run_id)
assert resumed.model_calls == 0
```

For a real endpoint, build a `RoutePool` over `OpenAIChatClient` (see
`agent_ultra.routes`) and pass it to `UltracodeEngine(pool)`.

---

## Troubleshooting

| symptom | fix |
|---------|-----|
| `no workflow named 'X'` | `agent-ultra ultracode list` to see valid names; or pass a `.py` path. |
| `run` hangs or errors on a real endpoint | Try `--mock` first to isolate the engine from the network, then `agent-ultra doctor --live` to probe your endpoint. |
| every agent fails: `all routes failed` | Your model routes are unreachable. Check `base_url`/routes in `agent-ultra.yaml` and that the key env var is set. `--mock` needs neither. |
| garbled dots/box characters | Force plain output: `AGENT_ULTRA_PLAIN=1` (or it's automatic when piped). |
| run ended as **HOLD** | It hit `--max-calls`/`--token-budget`. Raise the ceiling or narrow the workflow; partial results are journaled and resumable. |
| schema agent returns `None` | The model didn't produce valid JSON after retries — inspect `agents/aNNN.json` for its raw output; loosen the schema or the prompt. |
| where are my runs? | `~/.agent-ultra/ultracode/runs/` (or under `$AGENT_ULTRA_HOME`). `ultracode-log.jsonl` lists every run. |

Everything above is exercised by `agent-ultra doctor` and the offline test
suite (`pytest -q`), so if those pass, the pattern works on your machine.
