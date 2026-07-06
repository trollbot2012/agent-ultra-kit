# Troubleshooting

Run `agent-ultra doctor` first — it names the failing layer.

## Install

**"Python 3.10+ not found"** — install from python.org (Windows: tick *Add
python.exe to PATH*). Debian/Ubuntu also need `sudo apt install python3-venv`.

**`irm ... | iex` blocked** — some orgs block remote script execution.
Download `install.ps1`, read it, then run
`powershell -ExecutionPolicy Bypass -File install.ps1`.

**pip SSL / proxy errors** — corporate proxies often break `pip install git+…`.
Clone the repo and `pip install -e .` instead.

**`agent-ultra` not recognized after install** — the installer added a shim to
your *user* PATH; open a **new** terminal. Or call it by full path:
`%USERPROFILE%\.agent-ultra\bin\agent-ultra.cmd` (Windows) /
`~/.agent-ultra/venv/bin/agent-ultra` (Linux/macOS).

## Doctor lines

**`model route configured … NOT set` (WARN)** — harmless offline. Set the env
var named by `api_key_env` (see `.env.example`) and rerun with `--live`.

**`live route probe: 0/N healthy`** — the endpoint is down or wrong.
`base_url` must be the API root ending in `/v1`, `routes` must be names your
endpoint actually serves. Test by hand:
`curl http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $KEY"`.

**`artifact dir writable` FAIL** — you're in a read-only directory; set
`artifact_dir` in `agent-ultra.yaml` to somewhere writable.

**`command broker` FAIL** — on Windows the broker shells through
`powershell`; if PowerShell is unavailable/blocked, pass your own
`shell_argv` to `CommandBroker`, or fix PATH.

**`secret hygiene` FAIL** — a demo artifact contained a secret-shaped string.
This should never happen with kit-generated content; if you reproduced it,
please open an issue with the offending (redacted!) pattern.

## Runtime

**`PanelError: all model routes unhealthy`** — same as the live-probe failure
above; the report includes per-route health. The panel refuses to run rather
than fabricate findings.

**Panel returns weak "go gather info" findings** — you ran it without source.
Pass `--evidence-dir ./src` / `context=`; low-context runs are flagged and the
ULTRA loop refuses them outright.

**`denied` on a command you wanted to run** — that's the deny-by-default rule
for DANGEROUS commands with no approval path. Wire an `approver`, a sandbox
(`docker_sandbox_argv`), or — if you accept the risk — construct the broker
with `allow_dangerous_without_approval=True` to park instead of deny. It will
still never auto-run.

**Empty model responses / `reasoning starvation`** — reasoning models can
spend the whole token budget thinking. The client already retries with a
doubled budget; if it persists, raise the per-phase `max_tokens_*` config or
use a non-reasoning model for critics.

**ULTRA demo "HOLD" instead of ship** — correct behavior offline: the
deterministic mock re-panel repeats its findings, so the ship gate holds.
Against a real model an applied fix clears the finding.

## Uninstall / rollback

See [INSTALL.md](../INSTALL.md#rollback--uninstall). One-command installs live
entirely under `~/.agent-ultra`; pip installs uninstall with
`pip uninstall agent-ultra-kit`; `agent-ultra init` only ever creates
`agent-ultra.yaml`, `.env.example`, and `panel-runs/` — delete them to reset.
