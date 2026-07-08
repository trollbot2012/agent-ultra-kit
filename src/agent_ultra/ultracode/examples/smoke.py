"""Smoke workflow: two parallel agents + a two-stage pipeline. Proves
fan-out, journaling, and the receipt end to end. Runs offline under --mock.

    agent-ultra ultracode run smoke --mock
"""

META = {
    "name": "smoke",
    "description": "2 parallel pings + a 2-stage pipeline; end-to-end check",
    "phases": ["Ping", "Chain"],
}


async def run(wf):
    wf.phase("Ping")
    replies = await wf.parallel([
        lambda: wf.agent("Reply with exactly: pong-one", label="ping-1"),
        lambda: wf.agent("Reply with exactly: pong-two", label="ping-2"),
    ])

    wf.phase("Chain")
    chained = await wf.pipeline(
        ["alpha", "bravo"],
        lambda word: wf.agent(
            f"Return a JSON object with one key 'upper' whose value is "
            f"'{word}' uppercased.",
            schema={"type": "object",
                    "properties": {"upper": {"type": "string"}},
                    "required": ["upper"]},
            label=f"upper:{word}"),
        lambda parsed, word: {"word": word, "upper": (parsed or {}).get("upper")},
    )

    return {"pings": replies,
            "chained": [c for c in chained if c],
            "calls_spent": wf.budget.calls}
