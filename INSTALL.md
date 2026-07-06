# Installing agent-ultra-kit

Three ways in, all ending at the same place: `agent-ultra doctor` green and
`agent-ultra demo` running the full loop offline. No API key is needed to
install or verify — the mock route proves everything locally.

## 1. One-command install

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.ps1 | iex
```

**Linux / macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.sh | bash
```

**Windows (CMD):**

```bat
curl -fsSL https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.cmd -o install.cmd && install.cmd
```

What the installers do (and nothing else):

- check for Python ≥ 3.10
- create a private venv at `~/.agent-ultra/venv`
- `pip install` the kit from this repo
- put an `agent-ultra` shim on your user PATH
- run `agent-ultra doctor` to prove the install

**Rollback / uninstall:**

- Windows: `Remove-Item -Recurse -Force "$env:USERPROFILE\.agent-ultra"`
  (and remove `%USERPROFILE%\.agent-ultra\bin` from your user PATH)
- Linux/macOS: `rm -rf ~/.agent-ultra ~/.local/bin/agent-ultra`

Nothing outside `~/.agent-ultra` (plus the PATH entry / symlink) is touched.

## 2. pip / uv / clone install

```bash
pip install git+https://github.com/trollbot2012/agent-ultra-kit.git
# or
uv pip install git+https://github.com/trollbot2012/agent-ultra-kit.git
# or from a clone (dev mode, with tests):
git clone https://github.com/trollbot2012/agent-ultra-kit.git
cd agent-ultra-kit
pip install -e ".[dev]"
pytest -q
```

Zero runtime dependencies — stdlib only.

## 3. AI-agent handoff install

Paste this prompt into your own AI coding agent (Claude Code, Cursor, aider,
your own agent — anything that can run shell commands):

> Install agent-ultra-kit into this project and prove it works.
>
> 1. Install: `pip install git+https://github.com/trollbot2012/agent-ultra-kit.git`
>    (use the project's venv if there is one).
> 2. Scaffold config: run `agent-ultra init` in the project root. Expected new
>    files: `agent-ultra.yaml`, `.env.example`, and a `panel-runs/` directory.
> 3. Configure the model route: edit `agent-ultra.yaml` — set `base_url` to my
>    OpenAI-compatible endpoint, `routes` to my model name(s), and make sure
>    the env var named by `api_key_env` holds my key. Do NOT write the key
>    into any file. If I have no endpoint, skip this — the mock route works.
> 4. Health check: run `agent-ultra doctor` and show me the output. Every line
>    must be PASS (WARN on the model-route line is fine if I skipped step 3).
>    If configured, also run `agent-ultra doctor --live`.
> 5. Prove the loop: run `agent-ultra demo` and show me the output. It must
>    end with "DEMO PASSED".
> 6. Try it on real code:
>    `agent-ultra panel "Is this module production-safe?" --evidence-dir ./src`
>    (add `--mock` if no endpoint is configured).
> 7. If anything fails, read `docs/troubleshooting.md` in the kit repo, fix,
>    and rerun. Rollback if I ask: `pip uninstall agent-ultra-kit` and delete
>    `agent-ultra.yaml`, `.env.example`, `panel-runs/`.

## Verify (any install path)

```bash
agent-ultra doctor          # env, permissions, broker, panel, ULTRA, secrets
agent-ultra demo            # full offline loop, ends with DEMO PASSED
agent-ultra init            # scaffold config into the current project
agent-ultra classify "rm -rf build"       # DANGEROUS: deletes files
agent-ultra --mock panel "Is this safe?"  # offline panel
```

Doctor checks: Python version, package import, config/route + key env, write
permissions + artifact dir, command broker (safe auto-runs / dangerous denies
/ ledger written), panel demo, ULTRA demo, and secret hygiene (redaction
active, nothing leaked into artifacts).

## Configure a real model

`agent-ultra init`, then edit `agent-ultra.yaml`:

```yaml
base_url: http://127.0.0.1:4000/v1   # your OpenAI-compatible endpoint
api_key_env: OPENAI_API_KEY          # NAME of the env var with your key
routes: my-model-a, my-model-b       # what your endpoint serves
```

Works with OpenAI, LiteLLM, vLLM, Ollama, LM Studio, llama.cpp server — any
`/chat/completions` endpoint. Then `agent-ultra doctor --live`.

Troubles? See [docs/troubleshooting.md](docs/troubleshooting.md).
