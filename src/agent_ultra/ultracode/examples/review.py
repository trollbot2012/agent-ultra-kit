"""Adversarial review workflow: parallel finders (one per lens) hunt problems
in a target file/dir, findings are deduped, then each faces independent
skeptic votes -- only findings a majority fail to refute survive. The
canonical fan-out -> verify -> synthesize shape.

    agent-ultra ultracode run review --mock --args '{"target": "./src"}'

args (object):
    target   (required) file or directory to review
    question (optional) what to review for
    lenses   (optional) finder lenses (default 4 below)
    votes    (optional) skeptics per finding (default 3, majority rules)
    max_files (optional) evidence cap (default 8)
"""

from pathlib import Path

META = {
    "name": "review",
    "description": "fan-out finders -> dedupe -> per-finding skeptic votes -> synthesis",
    "phases": ["Find", "Verify", "Synthesize"],
}

DEFAULT_LENSES = ["correctness", "security", "failure-modes", "data-integrity"]

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "severity": {"type": "string",
                                 "enum": ["critical", "high", "medium", "low"]},
                    "anchor": {"type": "string"},
                },
                "required": ["claim", "severity"],
            },
        },
    },
    "required": ["findings"],
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "refuted": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["refuted", "reasoning"],
}

SKEPTIC_ANGLES = [
    "Assume the claim is WRONG and find the evidence that disproves it.",
    "Refute it if the impact is theoretical or already mitigated.",
    "Refute it if the cited anchor does not actually support the claim.",
]


def _pick_files(target: Path, cap: int) -> list:
    if target.is_file():
        return [target]
    exts = {".py", ".js", ".ts", ".md", ".yaml", ".yml", ".json", ".toml"}
    picked = [p for p in sorted(target.rglob("*"))
              if p.is_file() and p.suffix.lower() in exts
              and "node_modules" not in p.parts and ".git" not in p.parts]
    picked.sort(key=lambda p: p.stat().st_size, reverse=True)
    return picked[:cap]


def _dedupe(findings: list) -> list:
    kept = []
    for f in findings:
        words = set(f["claim"].lower().split())
        dup = any(len(words & set(k["claim"].lower().split()))
                  / max(1, len(words | set(k["claim"].lower().split()))) > 0.6
                  for k in kept)
        if not dup:
            kept.append(f)
    return kept


async def run(wf):
    a = wf.args if isinstance(wf.args, dict) else {}
    target = Path(str(a.get("target", ".") or ".")).expanduser()
    if not target.exists():
        raise ValueError(f"args.target does not exist: {target}")
    question = a.get("question") or (
        "Review this material for real defects: correctness bugs, security "
        "holes, data-loss paths, and operational failure modes.")
    lenses = list(a.get("lenses") or DEFAULT_LENSES)
    votes = max(1, int(a.get("votes", 3)))
    cap = int(a.get("max_files", 8))

    files = _pick_files(target, cap)
    if not files:
        return {"confirmed": [], "note": f"no reviewable files under {target}"}
    evidence = wf.evidence(files, max_files=cap)

    wf.phase("Find")
    finder_results = await wf.parallel([
        (lambda lens=lens: wf.agent(
            f"You are a {lens} critic. {question}\n"
            f"Report at most 4 findings through the {lens} lens ONLY, each with "
            "a short verbatim anchor from the evidence.\n\nEVIDENCE:\n" + evidence,
            schema=FINDINGS_SCHEMA, label=f"find:{lens}", phase="Find"))
        for lens in lenses])
    all_findings = []
    for lens, res in zip(lenses, finder_results):
        for f in (res or {}).get("findings", []):
            all_findings.append({**f, "lens": lens})
    findings = _dedupe(all_findings)
    if not findings:
        return {"confirmed": [], "note": "no findings survived dedupe"}

    wf.phase("Verify")

    async def verify(finding, _item, idx):
        ballots = await wf.parallel([
            (lambda k=k: wf.agent(
                f"A reviewer claims ({finding['lens']}, {finding['severity']}): "
                f"{finding['claim']}\nSkeptic angle: "
                f"{SKEPTIC_ANGLES[k % len(SKEPTIC_ANGLES)]}\n"
                "Try hard to REFUTE it. If you cannot, refuted=false.\n\n"
                "EVIDENCE:\n" + evidence,
                schema=VERDICT_SCHEMA, label=f"verify:{idx}:{k}", phase="Verify"))
            for k in range(votes)])
        real = [b for b in ballots if b]
        refutes = sum(1 for b in real if b.get("refuted"))
        return {**finding, "votes": len(real), "refuted_by": refutes,
                "confirmed": bool(real) and refutes <= len(real) // 2}

    judged = [j for j in await wf.pipeline(findings, verify) if j]
    confirmed = [j for j in judged if j["confirmed"]]

    wf.phase("Synthesize")
    synthesis = None
    if confirmed:
        listing = "\n".join(f"- ({c['lens']}/{c['severity']}) {c['claim']}"
                            for c in confirmed)
        synthesis = await wf.agent(
            "These findings each survived independent skeptic votes:\n"
            f"{listing}\n\nRank them by urgency; one line each, most urgent first.",
            label="synthesis")

    return {"target": str(target),
            "files_reviewed": [str(f) for f in files],
            "confirmed": confirmed,
            "rejected": [j for j in judged if not j["confirmed"]],
            "synthesis": synthesis}
