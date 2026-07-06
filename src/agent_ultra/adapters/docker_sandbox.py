"""Optional Docker sandbox adapter.

Provides a `sandbox_argv` callable for CommandBroker so that non-approved
(elevated/dangerous) commands run inside a throwaway container instead of the
host — satisfying the broker's "approval OR sandbox" clause.

No dependency on the docker SDK — it shells out to the `docker` CLI. If Docker
is not installed the broker's execution simply fails and is recorded as an
error (never silently "passed").
"""

from __future__ import annotations

import shlex


def docker_sandbox_argv(image: str = "python:3.12-slim",
                        workdir: str = "/work", mount: str = "",
                        network: str = "none"):
    """Return a callable(command) -> argv that runs the command in a
    disposable container. `mount`: host path bind-mounted read-write at
    workdir (omit for no mount). `network`: docker network mode (default
    'none' — no network egress from sandboxed commands)."""
    def argv(command: str):
        base = ["docker", "run", "--rm", "-i", f"--network={network}",
                "-w", workdir]
        if mount:
            base += ["-v", f"{mount}:{workdir}"]
        base += [image, "/bin/sh", "-c", command]
        return base
    argv.describe = lambda: (f"docker {image} net={network} "
                             f"mount={mount or '(none)'}")
    return argv


def parse_sandbox_command(command: str):
    """Helper: split a command safely for logging/debugging."""
    try:
        return shlex.split(command)
    except ValueError:
        return [command]
