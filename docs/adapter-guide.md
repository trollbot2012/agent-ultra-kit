# Adapter guide

The core knows nothing about your agent. To adopt the kit you implement a few
small seams. Most runtimes need only a `ChatClient` and (optionally) memory
hooks.

## Minimum: a model client

The only hard requirement is one method:

```python
class MyClient:
    def complete(self, model: str, prompt: str, max_tokens: int) -> str:
        ...   # call your model, return the text
```

If your models speak the OpenAI `/chat/completions` protocol (OpenAI,
LiteLLM, vLLM, Ollama, LM Studio, llama.cpp server, most hosted providers),
use the built-in `OpenAIChatClient` and skip this entirely.

```python
from agent_ultra.routes.client import OpenAIChatClient
from agent_ultra.routes.pool import RoutePool

pool = RoutePool(
    ["my-model-a", "my-model-b"],           # names your endpoint serves
    client=OpenAIChatClient("https://my-endpoint/v1", api_key_env="MY_KEY"))
```

`api_key_env` is the **name of an environment variable**, never the key
itself — so nothing secret lands in a committed config.

## Optional: memory hooks

Subclass `MemoryHooks` and override any of five events. All are no-ops by
default and exceptions are swallowed, so memory never breaks a run.

```python
from agent_ultra.memory.hooks import MemoryHooks

class MyMemory(MemoryHooks):
    def on_finding_accepted(self, finding): ...
    def on_task_complete(self, report): ...
    # on_panel_decision, on_command_run, on_lesson_learned
```

Pass it to `PanelEngine(pool, memory=MyMemory())` or the loop.

## Optional: execution approval / sandbox

For the broker, provide an `approver(BrokerResult) -> bool` (interactive gate)
and/or a `sandbox_argv(command) -> argv` (container). See
[command-broker.md](command-broker.md).

## Optional: build and fix seams

For the ULTRA loop, provide `builder(workspace, task) -> str` and
`fixer(fix_task, workspace) -> bool` that drive your coding agent. See
[ultra-loop.md](ultra-loop.md).

## Shipped adapters (all optional, none required by the core)

| adapter | module | what it does |
|---------|--------|--------------|
| generic CLI | `adapters.cli` | run panel/ultra/classify from the shell |
| LiteLLM | `adapters.litellm_routes` | build a pool over a LiteLLM proxy |
| Docker sandbox | `adapters.docker_sandbox` | `sandbox_argv` for the broker |
| memory (Mneme-style) | `adapters.mneme_memory` | wire hooks to a memory system |
| Hermes | `adapters.hermes` | env-driven pool + broker for Hermes-style agents |
| Ktisis | `adapters.ktisis` | env-driven ULTRA loop for Ktisis-style build agents |

Import only what you need — none of these are imported by the core, so a plain
`pip install agent-ultra-kit` pulls in **zero** third-party dependencies.

### Writing your own adapter

Adapters are allowed to be runtime-specific. Keep three rules:

1. Read endpoints, keys, and paths from **environment variables** — never
   hardcode a machine-specific path.
2. Depend on your runtime, not the other way around — the core must never
   import your adapter.
3. Fail loudly on missing config (e.g. "set `MYAGENT_ROUTES`"), don't
   silently no-op.
