"""bob surgical lane — the review step as an ultracode workflow.

Two agents examine the inert edit independently: one for accuracy (does the
text/config say what it should), one for safety (does the change smuggle in
anything with side effects — links, commands, config keys). The bob gate
cross-checks this run's receipt exactly like steps 6/7: fingerprint-pinned,
workflow-name-bound, and a claimed-but-never-run review blocks the commit.
"""

META = {
    "name": "bob-surgical-review",
    "description": "bob surgical lane: accuracy + safety review of an inert edit",
    "phases": ["Review", "Merge"],
}

LENSES = ("accuracy", "safety")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "concerns"]},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "notes"],
}


async def run(wf):
    args = wf.args if isinstance(wf.args, dict) else {}
    goal = str(args.get("goal", "") or "the edit")
    excerpt = str(args.get("target_excerpt", "") or "")[:6000]
    files = args.get("files") or []

    wf.phase("Review")
    results = await wf.parallel([
        (lambda lens=lens: wf.agent(
            f"Review this doc/config edit on the {lens} lens ONLY.\n"
            f"Task: {goal}\nFiles: {files}\nContent:\n{excerpt}\n"
            "Return your verdict and notes.",
            schema=REVIEW_SCHEMA, label=f"surgical:{lens}"))
        for lens in LENSES
    ])

    wf.phase("Merge", kind="fan_in")
    merged = {lens: (res or {"verdict": "concerns",
                             "notes": ["agent failed — treat as unreviewed"]})
              for lens, res in zip(LENSES, results)}
    return {"lenses": list(LENSES), "reviews": merged, "agents": len(LENSES)}
