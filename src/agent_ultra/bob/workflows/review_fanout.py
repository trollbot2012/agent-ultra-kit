"""bob step 7 — WORKFLOW review as an ultracode workflow.

One agent per review dimension (correctness, security, performance, style,
spec-compliance) examines the change independently; results are merged. The
bob gate cross-checks the ultracode run receipt and demands non-empty
dimensions — a claimed-but-never-run review blocks the commit.
"""

META = {
    "name": "bob-review",
    "description": "bob WORKFLOW step: multi-dimension review fan-out",
    "phases": ["Fanout", "Merge"],
}

DIMENSIONS = ("correctness", "security", "performance", "style",
              "spec-compliance")

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
    goal = str(args.get("goal", "") or "the change")
    spec = str(args.get("spec", "") or "")[:2000]
    excerpt = str(args.get("target_excerpt", "") or "")[:6000]

    wf.phase("Fanout")
    results = await wf.parallel([
        (lambda dim=dim: wf.agent(
            f"Review this change on the {dim} dimension ONLY.\n"
            f"Task: {goal}\nSpec: {spec}\nCode:\n{excerpt}\n"
            "Return your verdict and notes.",
            schema=REVIEW_SCHEMA, label=f"review:{dim}"))
        for dim in DIMENSIONS
    ])

    wf.phase("Merge", kind="fan_in")
    merged = {dim: (res or {"verdict": "concerns",
                           "notes": ["agent failed — treat as unreviewed"]})
              for dim, res in zip(DIMENSIONS, results)}
    return {"dimensions": list(DIMENSIONS), "reviews": merged,
            "agents": len(DIMENSIONS)}
