"""Generic CLI adapter — run a panel, ULTRA loop, or broker classification
from the shell against any OpenAI-compatible endpoint (or fully offline with
the mock route).

    agent-ultra panel "Is this safe?" --evidence-dir ./src
    agent-ultra ultra "add login" --workspace . --risk high --test-cmd "pytest -q"
    agent-ultra classify "rm -rf build"
    agent-ultra panel "..." --mock          # offline, deterministic

Routes/keys come from env + flags — never committed config:
    AGENT_ULTRA_BASE_URL   default http://127.0.0.1:4000/v1
    AGENT_ULTRA_API_KEY_ENV name of the env var holding the key (default OPENAI_API_KEY)
    AGENT_ULTRA_ROUTES     comma-separated model names (default: gpt-4o-mini)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..routes.client import OpenAIChatClient
from ..routes.mock import demo_panel_client
from ..routes.pool import RoutePool
from ..panel.engine import PanelEngine, PanelError
from ..broker.broker import CommandBroker, TRUSTED_OWNER_TIERS, classify
from ..artifacts.records import RunRecord, write_run


def _build_pool(args) -> RoutePool:
    if args.mock:
        client = demo_panel_client()
        routes = ["mock-a", "mock-b"] if args.mode == "mixed" else ["mock-a"]
        return RoutePool(routes, client=client)
    from .doctor import effective_config
    cfg = effective_config(getattr(args, "config", "") or "")
    client = OpenAIChatClient(base_url=cfg["base_url"],
                              api_key_env=cfg["api_key_env"])
    return RoutePool(cfg["routes"], client=client)


def _panel(args) -> int:
    pool = _build_pool(args)
    engine = PanelEngine(pool, config={"routing_mode": args.mode})
    try:
        report = engine.run(
            args.question, size=args.size,
            lenses=[x.strip() for x in args.lenses.split(",")] if args.lenses else None,
            context=Path(args.context_file).read_text(encoding="utf-8")
            if args.context_file else "",
            evidence_dirs=args.evidence_dir or [],
            allow_large=args.allow_large, mode=args.mode)
    except PanelError as e:
        print(f"panel error: {e}", file=sys.stderr)
        return 2
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(report), indent=2, default=str, ensure_ascii=False))
    else:
        print(f"\nDECISION: {report.decision}\n")
        print(f"verdicts: {report.verdict_counts()}")
        for f in report.accepted:
            print(f"  [{f.verdict}/{f.severity}] ({f.lens}) {f.claim}")
        if report.proof_gates:
            print("\nproof gates:")
            for g in report.proof_gates:
                print(f"  $ {g}")
        if report.destructive_gates:
            print("\nDESTRUCTIVE (needs a human):")
            for g in report.destructive_gates:
                print(f"  ! {g}")
    if args.out:
        rec = RunRecord(kind="panel", question=report.question,
                        run_id=report.run_id, routes=report.routes,
                        lenses=report.lenses, decision=report.decision,
                        accepted=[_finding_dict(f) for f in report.accepted],
                        rejected=[_finding_dict(f) for f in report.findings
                                  if not f.accepted],
                        proof_gates=report.proof_gates,
                        destructive_gates=report.destructive_gates,
                        outputs={"synthesis": report.synthesis})
        jp, mp = write_run(rec, args.out)
        print(f"\nartifacts: {jp}  {mp}")
    return 0


def _finding_dict(f) -> dict:
    return {"lens": f.lens, "claim": f.claim, "severity": f.severity,
            "verdict": f.verdict, "reasoning": f.reasoning, "check": f.check}


def _ultra(args) -> int:
    from ..ultra_loop.loop import UltraLoop, default_test_runner
    from ..workers.select import resolve_worker_choice, select_worker
    from ..workers.adapt import worker_as_fixer, worker_as_builder
    pool = _build_pool(args)
    engine = PanelEngine(pool, config={"routing_mode": args.mode})
    broker = CommandBroker(ledger_path=Path(args.workspace) / ".ultra" / "broker.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)

    # -- worker selection (router default; deepagents optional) ------------
    model = pool.routes[0] if getattr(pool, "routes", None) else "mock-a"
    client = pool.client_for(model)
    fixer = builder = None
    fixer_choice = resolve_worker_choice("fixer", args.fixer or args.worker,
                                         args.task, args.risk)
    builder_choice = resolve_worker_choice("builder", args.builder or args.worker,
                                           args.task, args.risk,
                                           needs_build=args.build)
    tr = default_test_runner(broker)
    try:
        if not args.no_fix:
            fw = select_worker(fixer_choice, client=client, model=model,
                               base_url=_worker_base_url(args),
                               api_key=_worker_api_key(args))
            fixer = worker_as_fixer(fw, test_runner=tr, test_cmd=args.test_cmd,
                                    timeout=600)
        if args.build:
            bw = select_worker(builder_choice, client=client, model=model,
                               base_url=_worker_base_url(args),
                               api_key=_worker_api_key(args))
            builder = worker_as_builder(bw, test_cmd=args.test_cmd)
    except RuntimeError as e:   # deepagents extra not installed
        print(f"worker error: {e}")
        return 2

    loop = UltraLoop(args.workspace, panel=engine, broker=broker,
                     fixer=fixer, builder=builder)
    rep = loop.run(args.task, risk=args.risk, test_cmd=args.test_cmd,
                   evidence_dirs=args.evidence_dir or [],
                   do_fix=not args.no_fix, allow_large=args.allow_large)
    print(f"\nworker: fixer={fixer_choice}"
          + (f" builder={builder_choice}" if args.build else ""))
    print(f"SHIPPED: {rep.shipped}\n{rep.ship_reason}")
    print(f"artifacts: {rep.artifact_dir}")
    return 0 if rep.shipped else 1


def _worker_base_url(args) -> str:
    from .doctor import effective_config
    try:
        return effective_config(getattr(args, "config", "") or "")["base_url"]
    except Exception:
        return "http://127.0.0.1:4000/v1"


def _worker_api_key(args) -> str:
    import os
    from .doctor import effective_config
    try:
        env = effective_config(getattr(args, "config", "") or "")["api_key_env"]
        return os.environ.get(env, "")
    except Exception:
        return ""


def _classify(args) -> int:
    tier, why = classify(args.command)
    print(f"{tier.upper()}: {why}")
    return 0


def _panel_gate(args) -> int:
    from ..panel_receipt import gate_report
    allowed, msg = gate_report(args.run_dir)
    print(f"{'ALLOWED' if allowed else 'BLOCKED'}: {msg}")
    return 0 if allowed else 1


# --------------------------------------------------------------------------
# ultracode — deterministic multi-agent workflow engine
# --------------------------------------------------------------------------

def _ultracode_engine(args):
    from ..ultracode import UltracodeEngine, demo_pool
    if getattr(args, "mock", False):
        pool = demo_pool()
    else:
        pool = _build_pool(args)
    routes = getattr(pool, "routes", None) or ["mock-a"]
    try:
        usage_source = pool.client_for(routes[0])
    except Exception:
        usage_source = None
    cfg = {}
    if getattr(args, "max_calls", 0):
        cfg["max_calls"] = args.max_calls
    if getattr(args, "token_budget", 0):
        cfg["token_budget"] = args.token_budget
    return UltracodeEngine(pool, config=cfg, usage_source=usage_source)


def _ultracode_run(args) -> int:
    from ..ultracode import WorkflowError, summarize_events, render_card, print_card
    eng = _ultracode_engine(args)
    wf_args = None
    if getattr(args, "args", ""):
        try:
            wf_args = json.loads(args.args)
        except ValueError:
            wf_args = args.args  # bare text is a valid wf.args
    try:
        path = eng.resolve(args.workflow)
        rep = eng.run_script(path, args=wf_args,
                             resume_run_id=getattr(args, "resume", "") or "")
    except WorkflowError as e:
        print(f"ultracode error: {e}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        from dataclasses import asdict
        print(json.dumps(asdict(rep), indent=2, default=str, ensure_ascii=False))
        return 0 if rep.status == "completed" else 1
    print_card(summarize_events(rep.events_path))
    if rep.result is not None:
        print("Result: " + json.dumps(rep.result, default=str, ensure_ascii=False)[:600])
    print("Report: " + rep.artifact_json)
    print("Resume: agent-ultra ultracode resume " + rep.run_id)
    return 0 if rep.status == "completed" else 1


def _ultracode_list(args) -> int:
    eng = _ultracode_engine(args)
    listing = eng.list_scripts()
    if listing["saved"]:
        print(f"saved ({eng.scripts_dir()}):")
        for s in listing["saved"]:
            print(f"  {s['name']:<20} {s['description']}")
    else:
        print(f"no saved workflows yet — drop .py files into {eng.scripts_dir()}")
    print("bundled examples:")
    for s in listing["examples"]:
        print(f"  {s['name']:<20} {s['description']}")
    return 0


def _ultracode_status(args) -> int:
    from ..ultracode import summarize_events, render_card
    eng = _ultracode_engine(args)
    runs = eng.home / "runs"
    files = sorted(runs.glob(".status-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True) \
        if runs.exists() else []
    cards = []
    for f in files:
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        events = st.get("events_path") or str(runs / st.get("run_id", "?") / "events.jsonl")
        summary = summarize_events(events)
        if not summary.get("ok"):
            summary = {"run_id": st.get("run_id"), "name": st.get("name"),
                       "phase": st.get("phase"), "counts": st.get("counts") or {},
                       "dots": []}
        cards.append(render_card(summary, rich=False))
    if not cards:
        print("No ultracode run is currently in progress.")
        return 0
    print("\n\n".join(cards))
    return 0


def _ultracode_resume(args) -> int:
    args.workflow = args.workflow_hint or _infer_workflow_for_run(args)
    if not args.workflow:
        print("could not infer which workflow produced run "
              f"{args.run_id!r}; pass it: "
              "agent-ultra ultracode resume <run_id> <workflow>", file=sys.stderr)
        return 2
    args.resume = args.run_id
    return _ultracode_run(args)


def _infer_workflow_for_run(args) -> str:
    """Read the run's journal run_start entry to recover the script path."""
    eng = _ultracode_engine(args)
    journal = eng.home / "runs" / args.run_id / "journal.jsonl"
    try:
        for line in journal.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry.get("type") == "run_start" and entry.get("script"):
                return entry["script"]
    except (OSError, ValueError):
        pass
    return ""


# --------------------------------------------------------------------------
# bob — the 10-step enforced build pipeline
# --------------------------------------------------------------------------

def _bob_run(args) -> int:
    from ..bob import run_bob
    pool = None
    if not args.mock:
        pool = _build_pool(args)
    outcome = run_bob(args.task, args.workspace, mock=args.mock, pool=pool,
                      interactive=False if args.no_quiz else None,
                      force=args.force)
    return 0 if outcome.passed else 1


def _bob_gate(args) -> int:
    from ..bob import BobRun, gate_check
    run = BobRun.active(args.workspace)
    if run is None:
        # non-interference by default; AGENT_ULTRA_REQUIRE_RUN=1 makes the
        # pipeline mandatory for hook-level commits (mirrors the private
        # reference's require-run policy).
        if args.hook and os.environ.get("AGENT_ULTRA_REQUIRE_RUN") == "1":
            print("BLOCKED: no active bob run and AGENT_ULTRA_REQUIRE_RUN=1 "
                  "— the pipeline is mandatory; start a run first",
                  file=sys.stderr)
            return 1
        print("no active bob run — nothing to gate (non-interference)")
        return 0
    result = gate_check(run, allow_mock=args.allow_mock,
                        rerun_tests=not args.no_rerun)
    if result.passed:
        if args.complete_on_pass:
            run.complete(result.to_dict())
        elif args.mark_pass:
            run.mark_pass(result.to_dict())
        print(f"gate PASSED for run {run.run_id}")
        for step, summary in result.checked.items():
            print(f"  [ok] {step}: {summary}")
        return 0
    print(f"gate BLOCKED run {run.run_id} — {len(result.errors)} failure(s):")
    for e in result.errors:
        print(f"  ! {e}")
    return 1


def _bob_seal(args) -> int:
    from ..bob import BobRun
    run = BobRun.active(args.workspace)
    if run is None:
        if args.if_passed:
            return 0
        print("no active bob run", file=sys.stderr)
        return 2
    if run.seal():
        print(f"SEALED — run {run.run_id}")
        return 0
    if args.if_passed:
        return 0
    print(f"NOT SEALED — run {run.run_id} has no recorded gate pass "
          "(run `agent-ultra bob gate --mark-pass` first)", file=sys.stderr)
    return 1


def _bob_hooks(args) -> int:
    from ..bob.pipeline import install_hooks
    try:
        hook = install_hooks(args.workspace, python=args.python)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"installed: {hook} (+ pre-merge-commit, post-commit)")
    print("lifecycle: pre-commit validates + marks the pass; post-commit "
          "seals after the commit lands.")
    return 0


def _bob_status(args) -> int:
    from ..bob import BobRun, STEPS, GATED_STEPS
    run = BobRun.active(args.workspace)
    if run is None:
        print("no active bob run")
        return 0
    print(f"run {run.run_id} — {run.goal()}")
    have = {r.get('step'): r for r in run.chain()}
    for step in STEPS:
        if step == "step10_commit":
            continue
        mark = ("done" if step in have
                else "REQUIRED" if step in GATED_STEPS else "-")
        print(f"  {step:<20} {mark}")
    missing = [s for s in GATED_STEPS if s not in have]
    print(f"\n{'ready for `agent-ultra bob gate`' if not missing else 'missing: ' + ', '.join(missing)}")
    return 0


def _receipts_bus(args):
    from ..receipts_bus import ReceiptsBus
    return ReceiptsBus(db_path=args.db or "", key_path=args.key or "")


def _receipts(args) -> int:
    bus = _receipts_bus(args)
    sub = args.receipts_cmd
    if sub == "list":
        rows = bus.list(session_id=args.session or "", workspace=args.workspace or "")
        for r in rows:
            print(f"  {r['receipt_id'][:12]} {r['kind']:<12} {r['verdict']:<10} "
                  f"{r.get('ts', '')}")
        print(f"\n{len(rows)} receipt(s)")
        return 0
    if sub == "show":
        r = bus.get(args.receipt_id)
        if r is None:
            print("not found", file=sys.stderr)
            return 1
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    if sub == "verify":
        res = bus.verify(args.receipt_id)
        print(f"ok={res['ok']} authentic={res['authentic']} "
              f"integrity={res['integrity']} refs_ok={res['refs_ok']}")
        return 0 if res["ok"] else 1
    if sub == "why":
        ctx = {"session_id": args.session or "", "workspace": args.workspace or "",
               "claim_sha256": args.claim_sha or "",
               "verify_command": args.verify_command or ""}
        if args.cited:
            ctx["cited"] = args.cited
        out = bus.why(ctx)
        for c in out["candidates"]:
            mark = "BINDS" if c["binds"] else "     "
            print(f"  [{mark}] {c['receipt_id'][:12] if c['receipt_id'] else '?':<12} "
                  f"{c['kind']:<12} {c['clause']} ({c['origin']})")
        print(f"\nclaim {'BINDS' if out['binds'] else 'is UNSUPPORTED'}")
        return 0 if out["binds"] else 1
    if sub == "attest":
        if not sys.stdin.isatty():
            print("attest is interactive-only; refusing in non-interactive mode",
                  file=sys.stderr)
            return 2
        note = input("attestation note: ").strip()
        env = bus.append(kind="manual", actor="operator", verdict="approved",
                         session_id=args.session or "",
                         workspace=args.workspace or "",
                         evidence=[{"type": "note", "value": note}])
        print(f"wrote manual receipt {env['receipt_id']} "
              f"(manual receipts never satisfy a completion gate)")
        return 0
    return 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent-ultra",
                                description="adversarial panel / ULTRA loop / broker")
    p.add_argument("--mock", action="store_true",
                   help="use the offline deterministic mock route")
    p.add_argument("--mode", default="single", choices=["single", "mixed"])
    p.add_argument("--config", default="",
                   help="path to agent-ultra.yaml (default ./agent-ultra.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("panel", help="run an adversarial panel")
    pp.add_argument("question")
    pp.add_argument("--size", default="small", choices=["small", "medium", "large"])
    pp.add_argument("--lenses", default="")
    pp.add_argument("--context-file", default="")
    pp.add_argument("--evidence-dir", action="append")
    pp.add_argument("--allow-large", action="store_true")
    pp.add_argument("--json", action="store_true")
    pp.add_argument("--out", default="")
    pp.set_defaults(func=_panel)

    up = sub.add_parser("ultra", help="run the build/test/panel/fix loop")
    up.add_argument("task")
    up.add_argument("--workspace", default=".")
    up.add_argument("--risk", default="medium", choices=["small", "medium", "high"])
    up.add_argument("--test-cmd", default="")
    up.add_argument("--evidence-dir", action="append")
    up.add_argument("--no-fix", action="store_true")
    up.add_argument("--allow-large", action="store_true")
    up.add_argument("--worker", default="auto",
                    choices=["auto", "router", "deepagents"],
                    help="worker runtime for builder+fixer (default auto: "
                         "router for fixes, deepagents for builds when installed)")
    up.add_argument("--builder", default="",
                    choices=["", "auto", "router", "deepagents"],
                    help="override the builder worker")
    up.add_argument("--fixer", default="",
                    choices=["", "auto", "router", "deepagents"],
                    help="override the fixer worker")
    up.add_argument("--build", action="store_true",
                    help="run the optional build phase before tests")
    up.set_defaults(func=_ultra)

    cp = sub.add_parser("classify", help="classify a command's risk tier")
    cp.add_argument("command")
    cp.set_defaults(func=_classify)

    # -- ultracode: deterministic multi-agent workflows -------------------
    ucp = sub.add_parser("ultracode",
                         help="run deterministic multi-agent workflows "
                              "(fan-out -> journal -> resume -> receipt)")
    ucsub = ucp.add_subparsers(dest="ultracode_cmd", required=True)

    def _uc_common(sp):
        # accept --mock/--mode/--config after the subcommand too, so the
        # friend-facing `agent-ultra ultracode run X --mock` form just works
        # (argparse otherwise requires global flags before the subcommand).
        sp.add_argument("--mock", action="store_true",
                        help="use the offline deterministic mock route (no key)")
        sp.add_argument("--mode", default="single", choices=["single", "mixed"])
        sp.add_argument("--config", default="")

    ucr = ucsub.add_parser("run", help="run a workflow by name or path")
    ucr.add_argument("workflow", help="saved name, bundled example, or a .py path")
    ucr.add_argument("--args", default="",
                     help="JSON (or bare text) passed to the workflow as wf.args")
    ucr.add_argument("--resume", default="",
                     help="run_id of a prior run to replay cached calls from")
    ucr.add_argument("--max-calls", type=int, default=0, dest="max_calls")
    ucr.add_argument("--token-budget", type=int, default=0, dest="token_budget")
    ucr.add_argument("--json", action="store_true")
    _uc_common(ucr)
    ucr.set_defaults(func=_ultracode_run)

    ucl = ucsub.add_parser("list", help="list saved + bundled workflows")
    _uc_common(ucl)
    ucl.set_defaults(func=_ultracode_list)

    ucs = ucsub.add_parser("status", help="show in-progress runs (terminal-safe)")
    _uc_common(ucs)
    ucs.set_defaults(func=_ultracode_status)

    ucrs = ucsub.add_parser("resume", help="resume a prior run by run_id "
                                           "(replays cached calls)")
    ucrs.add_argument("run_id")
    ucrs.add_argument("workflow_hint", nargs="?", default="",
                      help="the workflow name/path (inferred from the journal "
                           "if omitted)")
    ucrs.add_argument("--args", default="")
    ucrs.add_argument("--max-calls", type=int, default=0, dest="max_calls")
    ucrs.add_argument("--token-budget", type=int, default=0, dest="token_budget")
    ucrs.add_argument("--json", action="store_true")
    _uc_common(ucrs)
    ucrs.set_defaults(func=_ultracode_resume)

    # -- bob: the 10-step enforced build pipeline --------------------------
    for alias in ("bob", "build"):
        kwargs = {"help": "10-step enforced build pipeline: every step "
                          "leaves a receipt; commit/report only when "
                          "the chain validates (alias: build)"} \
            if alias == "bob" else {}
        bp = sub.add_parser(alias, **kwargs)
        bsub = bp.add_subparsers(dest="bob_cmd", required=True)

        br = bsub.add_parser("run", help="run the whole pipeline on a task")
        br.add_argument("task")
        br.add_argument("--workspace", default=".")
        br.add_argument("--no-quiz", action="store_true",
                        help="record the quiz step as skipped (non-interactive)")
        br.add_argument("--force", action="store_true",
                        help="abandon an existing unproven active run and start fresh")
        br.add_argument("--mock", action="store_true",
                        help="offline demo on the bundled sample task (no key)")
        br.add_argument("--mode", default="single", choices=["single", "mixed"])
        br.add_argument("--config", default="")
        br.set_defaults(func=_bob_run)

        bg = bsub.add_parser("gate", help="validate the receipt chain of the "
                                          "active run (pre-commit chokepoint)")
        bg.add_argument("--workspace", default=".")
        bg.add_argument("--allow-mock", action="store_true",
                        help="accept mock-route receipts (demo runs only)")
        bg.add_argument("--no-rerun", action="store_true",
                        help="skip the live pytest re-run (chain checks only)")
        bg.add_argument("--mark-pass", action="store_true",
                        help="record gate-pass.json on success, keep ACTIVE "
                             "(pre-commit hook mode — seal happens post-commit)")
        bg.add_argument("--complete-on-pass", action="store_true",
                        help="mark pass AND release ACTIVE on success "
                             "(manual close outside a commit)")
        bg.add_argument("--hook", action="store_true",
                        help="hook mode: honor AGENT_ULTRA_REQUIRE_RUN=1 "
                             "when no run is active")
        bg.set_defaults(func=_bob_gate)

        bl = bsub.add_parser("seal", help="release ACTIVE after the commit "
                                          "landed (post-commit hook; requires "
                                          "a recorded gate pass)")
        bl.add_argument("--workspace", default=".")
        bl.add_argument("--if-passed", action="store_true",
                        help="exit 0 silently when no run is active or the "
                             "gate has not passed (hook mode)")
        bl.set_defaults(func=_bob_seal)

        bh = bsub.add_parser("hook-install",
                             help="write the pre-commit (gate --mark-pass), "
                                  "pre-merge-commit, and post-commit (seal) "
                                  "hooks")
        bh.add_argument("--workspace", default=".")
        bh.add_argument("--python", default="",
                        help="interpreter to bake into the hooks")
        bh.set_defaults(func=_bob_hooks)

        bs = bsub.add_parser("status", help="show which steps have receipts")
        bs.add_argument("--workspace", default=".")
        bs.set_defaults(func=_bob_status)

    gp = sub.add_parser("panel-gate",
                        help="block REPORT unless a valid panel execution "
                             "receipt proves real panel-agent calls happened")
    gp.add_argument("run_dir", help="ULTRA run dir (has panel_execution_receipt.json)")
    gp.set_defaults(func=_panel_gate)

    rp = sub.add_parser("receipts",
                        help="list/show/verify/why/attest authenticated receipts")
    rp.add_argument("--db", default="", help="receipts store path (default in-memory)")
    rp.add_argument("--key", default="", help="per-install HMAC key file path")
    rp.add_argument("--session", default="")
    rp.add_argument("--workspace", default="")
    rsub = rp.add_subparsers(dest="receipts_cmd", required=True)
    rsub.add_parser("list", help="list stored receipts")
    rs_show = rsub.add_parser("show", help="print one receipt as JSON")
    rs_show.add_argument("receipt_id")
    rs_ver = rsub.add_parser("verify", help="check integrity + authenticity")
    rs_ver.add_argument("receipt_id")
    rs_why = rsub.add_parser("why", help="explain which receipts bind a claim")
    rs_why.add_argument("--claim-sha", dest="claim_sha", default="")
    rs_why.add_argument("--verify-command", dest="verify_command", default="")
    rs_why.add_argument("--cited", default="")
    rsub.add_parser("attest", help="write a manual receipt (interactive only)")
    rp.set_defaults(func=_receipts)

    from .doctor import run_doctor, run_init, run_demo

    dp = sub.add_parser("doctor", help="verify the install end to end")
    dp.add_argument("--live", action="store_true",
                    help="also probe the configured model endpoint")
    dp.add_argument("--deepagents", action="store_true",
                    help="also check the optional Deep Agents worker extra")
    dp.set_defaults(func=run_doctor)

    ip = sub.add_parser("init", help="scaffold config + artifact dirs here")
    ip.add_argument("--dir", default=".")
    ip.add_argument("--force", action="store_true")
    ip.set_defaults(func=run_init)

    mp = sub.add_parser("demo", help="run panel + broker + ULTRA offline")
    mp.set_defaults(func=run_demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
