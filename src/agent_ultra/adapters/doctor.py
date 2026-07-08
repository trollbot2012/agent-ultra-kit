"""Self-installer, health checker, and offline demo.

    agent-ultra init      scaffold config + artifact dirs in the current dir
    agent-ultra doctor    verify the install end to end (offline by default)
    agent-ultra demo      run the panel + broker + ULTRA loop offline

The doctor proves a working install WITHOUT any API key: the panel and ULTRA
checks run on the deterministic mock route. A configured live route is probed
only with --live (so doctor never hangs on a dead endpoint by default).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

CONFIG_TEMPLATE = """\
# agent-ultra.yaml — flat key: value config (env vars AGENT_ULTRA_* override).
# SAFE DEFAULTS: no key here, only the NAME of the env var that holds it.

# OpenAI-compatible endpoint (OpenAI, LiteLLM, vLLM, Ollama, LM Studio...)
base_url: http://127.0.0.1:4000/v1

# Name of the environment variable holding your API key. NEVER the key itself.
api_key_env: OPENAI_API_KEY

# Comma-separated model names/aliases your endpoint serves.
routes: gpt-4o-mini

# single = one route runs every panel role; mixed = spread across routes.
mode: single

# Where panel/ULTRA artifacts are written.
artifact_dir: ./panel-runs
"""

ENV_TEMPLATE = """\
# Copy to .env (which is gitignored) and fill in. NEVER commit real keys.
# The kit reads the VARIABLE NAMED by api_key_env / AGENT_ULTRA_API_KEY_ENV.
OPENAI_API_KEY=replace-me
# Optional overrides (take precedence over agent-ultra.yaml):
# AGENT_ULTRA_BASE_URL=http://127.0.0.1:4000/v1
# AGENT_ULTRA_ROUTES=gpt-4o-mini
# AGENT_ULTRA_API_KEY_ENV=OPENAI_API_KEY
"""

_DEMO_SOURCE = '''"""Toy auth service (demo target)."""
SESSIONS = {}

def authenticate(request):
    # BUG: only checks the header KEY exists, not that the token is non-empty
    return "token" in request.headers

def login(user, token):
    SESSIONS[user] = {"token": token}
    return SESSIONS[user]

def logout(user):
    SESSIONS.pop(user, None)
'''


def load_simple_yaml(path) -> dict:
    """Flat `key: value` subset — comments (#) and blank lines ignored.
    Deliberately tiny so the kit stays stdlib-only."""
    cfg: dict = {}
    try:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            cfg[key.strip()] = val.strip()
    except OSError:
        pass
    return cfg


def effective_config(config_path: str = "") -> dict:
    """config file < env vars. Returns base_url/api_key_env/routes/mode/
    artifact_dir with safe defaults."""
    path = config_path or os.environ.get("AGENT_ULTRA_CONFIG", "agent-ultra.yaml")
    cfg = load_simple_yaml(path) if Path(path).exists() else {}
    out = {
        "base_url": os.environ.get("AGENT_ULTRA_BASE_URL")
        or cfg.get("base_url", "http://127.0.0.1:4000/v1"),
        "api_key_env": os.environ.get("AGENT_ULTRA_API_KEY_ENV")
        or cfg.get("api_key_env", "OPENAI_API_KEY"),
        "routes": [r.strip() for r in (os.environ.get("AGENT_ULTRA_ROUTES")
                                       or cfg.get("routes", "gpt-4o-mini")
                                       ).split(",") if r.strip()],
        "mode": os.environ.get("AGENT_ULTRA_MODE") or cfg.get("mode", "single"),
        "artifact_dir": os.environ.get("AGENT_ULTRA_ARTIFACT_DIR")
        or cfg.get("artifact_dir", "./panel-runs"),
        "config_file": str(path) if Path(path).exists() else "",
    }
    return out


# --------------------------------------------------------------------------
# demo pieces (offline, deterministic — shared by `demo` and `doctor`)
# --------------------------------------------------------------------------

def panel_demo(out_dir: Path):
    from ..routes.pool import RoutePool
    from ..routes.mock import demo_panel_client
    from ..panel.engine import PanelEngine
    pool = RoutePool(["mock-a"], client=demo_panel_client())
    report = PanelEngine(pool).run(
        "Is this authentication function safe for production?",
        size="small", lenses=["security", "correctness", "failure-modes"],
        context=_DEMO_SOURCE)
    ok = bool(report.findings) and bool(report.decision) and report.accepted
    return ok, report


def ultracode_demo(out_dir: Path):
    """Fan-out -> journal -> resume -> receipt, fully offline. Returns
    (ok, info) where ok also proves the resume replayed with zero calls and
    the receipt checksum validates."""
    from ..ultracode import UltracodeEngine, demo_pool, verify_receipt
    import json as _json
    home = out_dir / "ultracode-demo"
    eng = UltracodeEngine(demo_pool(), home=home)
    rep = eng.run_script(eng.resolve("smoke"))
    receipt = _json.loads(Path(rep.receipt_path).read_text(encoding="utf-8"))
    resumed = eng.run_script(eng.resolve("smoke"), resume_run_id=rep.run_id)
    ok = (rep.final_state == "COMPLETE"
          and rep.model_calls == 4
          and verify_receipt(receipt)
          and resumed.model_calls == 0
          and resumed.cached_hits == 4)
    return ok, {"final_state": rep.final_state, "calls": rep.model_calls,
                "resume_calls": resumed.model_calls,
                "resume_cached": resumed.cached_hits,
                "receipt_valid": verify_receipt(receipt),
                "run_dir": rep.artifact_dir}


def broker_demo(out_dir: Path):
    from ..broker.broker import CommandBroker, TRUSTED_OWNER_TIERS
    ledger = out_dir / "broker-demo.jsonl"
    b = CommandBroker(ledger_path=ledger, auto_run_tiers=TRUSTED_OWNER_TIERS)
    safe = b.run("echo agent-ultra-doctor")
    danger = b.run("rm -rf demo-guard")
    ok = (safe.status == "passed" and danger.status == "denied"
          and ledger.exists()
          and len(ledger.read_text(encoding="utf-8").strip().splitlines()) == 2)
    return ok, {"safe": safe.status, "dangerous": danger.status,
                "ledger": str(ledger)}


def ultra_demo(out_dir: Path):
    from ..routes.pool import RoutePool
    from ..routes.mock import demo_panel_client
    from ..panel.engine import PanelEngine
    from ..broker.broker import CommandBroker, TRUSTED_OWNER_TIERS
    from ..ultra_loop.loop import UltraLoop, TestResult

    ws = out_dir / "ultra-demo"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "service.py").write_text(_DEMO_SOURCE, encoding="utf-8")
    engine = PanelEngine(RoutePool(["mock-a"], client=demo_panel_client()))
    broker = CommandBroker(ledger_path=ws / ".ultra" / "broker.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)
    fixed = []

    def fake_tests(workspace, cmd, timeout):
        return TestResult(cmd or "pytest", 0, True, "16 passed", 0.1)

    def fake_fixer(task, workspace):
        fixed.append(task.claim)
        return True

    loop = UltraLoop(ws, panel=engine, broker=broker,
                     test_runner=fake_tests, fixer=fake_fixer)
    rep = loop.run("implement token authentication", risk="high",
                   test_cmd="pytest -q")
    # a correct offline run: tests green, panel ran, findings became fix
    # tasks, fixes were applied, artifacts + proof gates written, and the
    # gate HOLDS (the deterministic mock re-panel repeats its findings).
    ok = (rep.tests_before.get("passed") is True and rep.panel_ran
          and len(fixed) >= 1
          and any(t["status"] == "fixed" for t in rep.fix_tasks)
          and Path(rep.artifact_dir).exists())
    return ok, rep


def receipts_demo(out_dir: Path):
    """Offline: sign a receipt, prove authenticity is separate from integrity,
    reject a forgery, and verify a hash-chained gate audit."""
    from ..receipts_bus import ReceiptsBus
    from ..receipts_bus.envelope import receipt_sha256
    key = out_dir / "install.key"
    key.write_bytes(b"agent-ultra-doctor-install-key-0123456789")
    bus = ReceiptsBus(db_path=out_dir / "receipts.db", key_path=key)
    good = bus.append(kind="panel", actor="engine_a", verdict="shipped",
                      session_id="demo")
    gv = bus.verify(good["receipt_id"])
    # a hand-authored envelope with a valid sha256 but no HMAC is a forgery
    forged = dict(good)
    forged["receipt_id"] = "forged-0001"
    forged["receipt_hmac"] = ""
    forged["receipt_sha256"] = receipt_sha256(forged)
    bus.put(forged)
    fv = bus.verify("forged-0001")
    bus.append_audit("ship", "enforce", "allow", "engine_a", "demo evidence")
    bus.append_audit("ship", "enforce", "block", "reviewer", "demo stale")
    chain = bus.verify_audit("ship")
    ok = (gv["ok"] and gv["authentic"]
          and fv["integrity"] and not fv["authentic"] and not fv["ok"]
          and chain["ok"])
    return ok, {"genuine_ok": gv["ok"],
                "forgery_authentic": fv["authentic"],
                "audit_chain_ok": chain["ok"]}


def verifier_demo(out_dir: Path):
    """Offline: refute-first defaults, engine re-check via an injected runner,
    and a REFUTED verdict producing a non-acceptable receipt."""
    from ..verifier import verify_claim
    from ..receipts_bus import ReceiptsBus, Candidate
    key = out_dir / "verifier.key"
    key.write_bytes(b"agent-ultra-doctor-verifier-key-0123456789")
    bus = ReceiptsBus(db_path=out_dir / "verifier.db", key_path=key)

    def emit(fields):
        return bus.append(**fields)["receipt_id"]

    def run_check(cmd, cwd):
        return {"exit_code": 0, "ledger_ref": "demo://1"}

    no_evidence = verify_claim("it works")           # -> refuted (default)
    confirmed = verify_claim("tests pass", declared_verify_cmd="pytest -q",
                             run_check=run_check, session_id="demo",
                             emit_receipt=emit)
    refuted = verify_claim("bogus", session_id="demo", emit_receipt=emit)
    refuted_receipt = bus.get(refuted["receipt_id"])
    # the refuted receipt must NOT satisfy a gate for its own claim
    ok_bind, clause = bus.binds(
        {"session_id": "demo", "claim_sha256": refuted["claim_sha256"]},
        Candidate(receipt=refuted_receipt))
    ok = (no_evidence["refuted"] and not confirmed["refuted"]
          and refuted["refuted"] and refuted_receipt["verdict"] == "failed"
          and not ok_bind)
    return ok, {"no_evidence_refuted": no_evidence["refuted"],
                "recheck_confirmed": not confirmed["refuted"],
                "refuted_verdict": refuted_receipt["verdict"]}


def bob_demo(out_dir: Path):
    """Offline: the full 10-step pipeline passes its gate, then three faked
    chains block — a skipped step, a fabricated fan-out claim, and a doctored
    panel receipt. All state is isolated under *out_dir*."""
    import io
    from ..bob import run_bob, gate_check
    from ..bob.pipeline import BobRun

    ws = out_dir / "bob-demo"
    key = out_dir / "bob-demo.key"
    uc_home = out_dir / "bob-demo-ultracode"
    sink = io.StringIO()
    outcome = run_bob("add a slugify helper", ws, mock=True, key_path=key,
                      ultracode_home=uc_home, interactive=False, out=sink)

    run = BobRun(workspace=ws, run_dir=ws / ".agent-ultra" / "bob" / "runs"
                 / outcome.run_id, key=key.read_bytes())

    # 1. a skipped step blocks: remove the workflow receipt
    wf_receipt = run.receipt_path("step07_workflow")
    saved = wf_receipt.read_text(encoding="utf-8")
    wf_receipt.unlink()
    skipped_blocks = not gate_check(run, allow_mock=True,
                                    rerun_tests=False).passed
    wf_receipt.write_text(saved, encoding="utf-8")

    # 2. a fabricated fan-out claim blocks: re-sign step06 naming an invented
    # ultracode run (valid chain + HMAC, but nothing on disk backs the claim)
    sec = run.load("step06_security")
    run.receipt_path("step06_security").unlink()
    run.write("step06_security",
              {"ultracode": {"run_id": "20990101T000000-fake-000000",
                             "home": str(uc_home)},
               "lenses": ["invented"], "findings": []},
              writer="engine", mock=True)
    fake_fanout_blocks = not gate_check(run, allow_mock=True,
                                        rerun_tests=False).passed
    run.receipt_path("step06_security").write_text(
        json.dumps(sec, indent=2, ensure_ascii=False), encoding="utf-8")

    # 3. a doctored panel receipt blocks: flip one byte in the panel's own
    # execution receipt (checksum no longer matches)
    panel_receipt = run.run_dir / "panel" / "panel_execution_receipt.json"
    body = json.loads(panel_receipt.read_text(encoding="utf-8"))
    body["lens_count_executed"] = 99
    panel_receipt.write_text(json.dumps(body, indent=2), encoding="utf-8")
    fake_panel_blocks = not gate_check(run, allow_mock=True,
                                       rerun_tests=False).passed

    ok = (outcome.passed and skipped_blocks and fake_fanout_blocks
          and fake_panel_blocks)
    return ok, {"pipeline_passed": outcome.passed,
                "skipped_step_blocks": skipped_blocks,
                "fake_fanout_blocks": fake_fanout_blocks,
                "fake_panel_blocks": fake_panel_blocks,
                "run_id": outcome.run_id}


def run_demo(args) -> int:
    out = Path(tempfile.mkdtemp(prefix="agent-ultra-demo-"))
    print("agent-ultra demo (offline, deterministic mock route)\n")
    ok1, report = panel_demo(out)
    print(f"[{'ok' if ok1 else 'FAIL'}] panel: {len(report.findings)} findings, "
          f"{len(report.accepted)} accepted -> {report.decision[:70]}")
    ok2, b = broker_demo(out)
    print(f"[{'ok' if ok2 else 'FAIL'}] broker: safe={b['safe']}, "
          f"dangerous={b['dangerous']} (deny-by-default)")
    ok3, rep = ultra_demo(out)
    print(f"[{'ok' if ok3 else 'FAIL'}] ultra: tests green -> panel -> "
          f"{len(rep.fix_tasks)} fix task(s) -> fix loop -> proof saved")
    ok4, rb = receipts_demo(out)
    print(f"[{'ok' if ok4 else 'FAIL'}] receipts: genuine authentic, "
          f"forgery rejected (authentic={rb['forgery_authentic']}), "
          f"audit chain ok={rb['audit_chain_ok']}")
    ok5, vb = verifier_demo(out)
    print(f"[{'ok' if ok5 else 'FAIL'}] verifier: no-evidence refuted, "
          f"re-check confirmed, refuted verdict={vb['refuted_verdict']} "
          f"(cannot satisfy a gate)")
    ok6, uc = ultracode_demo(out)
    print(f"[{'ok' if ok6 else 'FAIL'}] ultracode: {uc['calls']} agents fan out "
          f"-> {uc['final_state']} -> receipt valid={uc['receipt_valid']} "
          f"-> resume replays {uc['resume_cached']} cached ({uc['resume_calls']} calls)")
    ok7, bb = bob_demo(out)
    print(f"[{'ok' if ok7 else 'FAIL'}] bob: 10-step pipeline gate "
          f"passed={bb['pipeline_passed']}; skipped step blocks="
          f"{bb['skipped_step_blocks']}, fake fan-out blocks="
          f"{bb['fake_fanout_blocks']}, doctored panel blocks="
          f"{bb['fake_panel_blocks']}")
    print(f"\nartifacts: {out}")
    all_ok = ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7
    print("\nDEMO " + ("PASSED — the full loop works on this machine."
                       if all_ok else "FAILED — run `agent-ultra doctor`."))
    return 0 if all_ok else 1


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def run_doctor(args) -> int:
    results: list[tuple[str, str, str]] = []

    def add(status, name, detail=""):
        results.append((status, name, detail))

    # 1. python version
    v = sys.version_info
    add(PASS if v >= (3, 10) else FAIL,
        f"python {v.major}.{v.minor}.{v.micro}", "need >= 3.10")

    # 2. package + version
    try:
        import agent_ultra
        add(PASS, f"agent_ultra {agent_ultra.__version__} importable")
    except Exception as e:
        add(FAIL, "agent_ultra import", str(e)[:120])
        _print(results)
        return 1

    # 3. config / route
    cfg = effective_config(getattr(args, "config", "") or "")
    src = cfg["config_file"] or "(defaults + env)"
    add(PASS, f"config source: {src}",
        f"routes={','.join(cfg['routes'])} base={cfg['base_url']}")
    key_set = bool(os.environ.get(cfg["api_key_env"], ""))
    if getattr(args, "live", False):
        try:
            from ..routes.client import OpenAIChatClient
            from ..routes.pool import RoutePool
            client = OpenAIChatClient(cfg["base_url"],
                                      api_key_env=cfg["api_key_env"], timeout=15)
            pool = RoutePool(cfg["routes"], client=client)
            healthy = pool.probe_all()
            add(PASS if healthy else FAIL,
                f"live route probe: {len(healthy)}/{len(cfg['routes'])} healthy",
                str(pool.health_report()["health"]))
        except Exception as e:
            add(FAIL, "live route probe", str(e)[:120])
    else:
        add(PASS if key_set else WARN,
            f"model route configured (key env {cfg['api_key_env']} "
            f"{'set' if key_set else 'NOT set'})",
            "offline mock works regardless; use --live to probe the endpoint")

    # 4. write permissions + artifact dir
    art = Path(cfg["artifact_dir"])
    try:
        art.mkdir(parents=True, exist_ok=True)
        probe = art / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(PASS, f"artifact dir writable: {art}")
    except OSError as e:
        add(FAIL, f"artifact dir writable: {art}", str(e)[:120])

    scratch = Path(tempfile.mkdtemp(prefix="agent-ultra-doctor-"))

    # 5. command broker enabled + deny-by-default intact
    try:
        ok, detail = broker_demo(scratch)
        add(PASS if ok else FAIL,
            "command broker: safe auto-runs, dangerous denies, ledger written",
            json.dumps(detail)[:120])
    except Exception as e:
        add(FAIL, "command broker", str(e)[:120])

    # 6. panel demo (offline)
    try:
        ok, report = panel_demo(scratch)
        add(PASS if ok else FAIL,
            f"panel demo: {len(report.findings)} findings, "
            f"{len(report.accepted)} accepted, decision produced")
    except Exception as e:
        add(FAIL, "panel demo", str(e)[:160])

    # 7. ultra demo (offline)
    try:
        ok, rep = ultra_demo(scratch)
        add(PASS if ok else FAIL,
            "ULTRA demo: build->test->panel->fix loop->proof artifacts")
    except Exception as e:
        add(FAIL, "ULTRA demo", str(e)[:160])

    # 7a2. ultracode: multi-agent fan-out -> journal -> resume -> receipt
    try:
        ok, detail = ultracode_demo(scratch)
        add(PASS if ok else FAIL,
            "ultracode: fan-out -> journal -> resume (cached) -> valid receipt",
            json.dumps(detail)[:140])
    except Exception as e:
        add(FAIL, "ultracode", str(e)[:160])

    # 7a3. bob: the 10-step pipeline passes its gate; faked chains block
    try:
        ok, detail = bob_demo(scratch)
        add(PASS if ok else FAIL,
            "bob: 10-step pipeline gate passes; skipped/faked steps block",
            json.dumps(detail)[:140])
    except Exception as e:
        add(FAIL, "bob pipeline", str(e)[:160])

    # 7b. receipts bus: authenticity separate from integrity, forgery rejected,
    # gate audit chain intact.
    try:
        ok, detail = receipts_demo(scratch)
        add(PASS if ok else FAIL,
            "receipts bus: HMAC authenticity, forgery rejected, audit chain",
            json.dumps(detail)[:120])
    except Exception as e:
        add(FAIL, "receipts bus", str(e)[:160])

    # 7c. verifier: refute-first default, engine re-check, non-acceptable
    # verdict on refutation.
    try:
        ok, detail = verifier_demo(scratch)
        add(PASS if ok else FAIL,
            "verifier: refute-first default, engine re-check, refuted!=gate",
            json.dumps(detail)[:120])
    except Exception as e:
        add(FAIL, "verifier", str(e)[:160])

    # 8. secret hygiene: redaction works, and nothing the demos wrote leaks
    try:
        from ..evidence.reader import redact_secrets, find_secrets
        canary = "sk-" + "canary0123456789abcdef"
        redacts = canary not in redact_secrets(f"key = {canary}")
        leaked = []
        for f in scratch.rglob("*"):
            if f.is_file():
                try:
                    leaked += find_secrets(f.read_text(encoding="utf-8",
                                                       errors="ignore"))
                except OSError:
                    pass
        add(PASS if (redacts and not leaked) else FAIL,
            "secret hygiene: redaction active, no secrets in artifacts",
            f"{len(leaked)} leak(s) found" if leaked else "")
    except Exception as e:
        add(FAIL, "secret hygiene", str(e)[:120])

    # 9. hybrid worker layer: router is always available; deepagents is an
    # OPTIONAL extra — its absence is a PASS (never required).
    try:
        from ..workers import RouterWorker  # noqa: F401
        from ..workers.deepagents_worker import deepagents_available
        if getattr(args, "deepagents", False):
            avail = deepagents_available()
            add(PASS if avail else WARN,
                f"deepagents worker: {'installed' if avail else 'NOT installed'}",
                "optional extra: pip install agent-ultra-kit[deepagents]"
                if not avail else "optional heavy builder/fixer ready")
        else:
            add(PASS, "worker layer: router (default) available",
                "deepagents is optional — run `doctor --deepagents` to check it")
    except Exception as e:
        add(FAIL, "worker layer", str(e)[:120])

    _print(results)
    return 1 if any(s == FAIL for s, _, _ in results) else 0


def _print(results) -> None:
    print("agent-ultra doctor\n")
    for status, name, detail in results:
        line = f"  [{status}] {name}"
        if detail:
            line += f"  — {detail}"
        print(line)
    n = {s: sum(1 for r in results if r[0] == s) for s in (PASS, WARN, FAIL)}
    print(f"\nResult: {n[PASS]} pass, {n[WARN]} warn, {n[FAIL]} fail")
    if n[FAIL]:
        print("See docs/troubleshooting.md")


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

def run_init(args) -> int:
    target = Path(getattr(args, "dir", ".") or ".").resolve()
    target.mkdir(parents=True, exist_ok=True)
    force = getattr(args, "force", False)
    wrote = []
    for name, content in (("agent-ultra.yaml", CONFIG_TEMPLATE),
                          (".env.example", ENV_TEMPLATE)):
        p = target / name
        if p.exists() and not force:
            print(f"  kept existing {p} (use --force to overwrite)")
            continue
        p.write_text(content, encoding="utf-8")
        wrote.append(p)
        print(f"  wrote {p}")
    art = target / "panel-runs"
    art.mkdir(exist_ok=True)
    print(f"  ensured {art}")
    print("\nNext steps:")
    print("  1. copy .env.example to .env and set your key (or skip — the")
    print("     mock route needs no key: try `agent-ultra demo`)")
    print("  2. edit agent-ultra.yaml (endpoint + model routes)")
    print("  3. run `agent-ultra doctor` (add --live to probe your endpoint)")
    print('  4. run `agent-ultra panel "your question" --evidence-dir ./src`')
    return 0
