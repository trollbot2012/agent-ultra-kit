"""bob step 6 — SECURITY-FANOUT as an ultracode workflow.

Independent security-focused agents (one per attack lens) each review the
same code and return structured findings. The ultracode engine journals every
call and writes its checksummed run receipt — that receipt is what the bob
gate cross-checks, so the fan-out cannot be claimed without having run.
"""

META = {
    "name": "bob-security",
    "description": "bob SECURITY-FANOUT: independent security lenses over the change",
    "phases": ["Fanout", "Merge"],
}

LENSES = ("injection-and-input-handling", "secrets-and-data-exposure",
          "resource-and-denial", "logic-and-authz")

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string",
                                 "enum": ["low", "medium", "high", "critical"]},
                    "triage": {"type": "string",
                               "enum": ["real_now", "real_later", "theoretical"]},
                    "claim": {"type": "string"},
                },
                "required": ["id", "severity", "triage", "claim"],
            },
        },
    },
    "required": ["findings"],
}


async def run(wf):
    args = wf.args if isinstance(wf.args, dict) else {}
    goal = str(args.get("goal", "") or "the change")
    excerpt = str(args.get("target_excerpt", "") or "")[:6000]

    wf.phase("Fanout")
    results = await wf.parallel([
        (lambda lens=lens: wf.agent(
            f"You are a security reviewer using ONLY the {lens} lens.\n"
            f"Task under review: {goal}\n"
            f"Code:\n{excerpt}\n"
            "Report concrete findings for this lens (empty list if none).",
            schema=FINDINGS_SCHEMA, label=f"sec:{lens}"))
        for lens in LENSES
    ])

    wf.phase("Merge", kind="fan_in")
    findings = []
    for lens, res in zip(LENSES, results):
        for f in ((res or {}).get("findings") or []):
            findings.append({**f, "lens": lens})
    return {"lenses": list(LENSES), "findings": findings,
            "agents": len(LENSES)}
