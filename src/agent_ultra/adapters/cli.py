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
    from ..ultra_loop.loop import UltraLoop
    pool = _build_pool(args)
    engine = PanelEngine(pool, config={"routing_mode": args.mode})
    broker = CommandBroker(ledger_path=Path(args.workspace) / ".ultra" / "broker.jsonl",
                           auto_run_tiers=TRUSTED_OWNER_TIERS)
    loop = UltraLoop(args.workspace, panel=engine, broker=broker)
    rep = loop.run(args.task, risk=args.risk, test_cmd=args.test_cmd,
                   evidence_dirs=args.evidence_dir or [],
                   do_fix=not args.no_fix, allow_large=args.allow_large)
    print(f"\nSHIPPED: {rep.shipped}\n{rep.ship_reason}")
    print(f"artifacts: {rep.artifact_dir}")
    return 0 if rep.shipped else 1


def _classify(args) -> int:
    tier, why = classify(args.command)
    print(f"{tier.upper()}: {why}")
    return 0


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
    up.set_defaults(func=_ultra)

    cp = sub.add_parser("classify", help="classify a command's risk tier")
    cp.add_argument("command")
    cp.set_defaults(func=_classify)

    from .doctor import run_doctor, run_init, run_demo

    dp = sub.add_parser("doctor", help="verify the install end to end")
    dp.add_argument("--live", action="store_true",
                    help="also probe the configured model endpoint")
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
