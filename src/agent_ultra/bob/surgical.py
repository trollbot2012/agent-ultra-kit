"""Surgical lane entry criteria — the lightweight tier for INERT edits.

The surgical lane exists for typo-class doc/config edits where the full
10-step pipeline is overkill. It never replaces Bob: every surgical check
is a HARDER, NARROWER constraint than full mode (declared-file allowlist,
inert file types, a denylist, a small diff budget), and anything that does
not qualify falls back to the FULL pipeline — the heavier lane is never
weakened by the lighter one existing.

Qualification is allowlist-first:

  * truly-inert PROSE (.md/.rst/.txt/...) and known-inert dotfiles
    auto-qualify;
  * STRUCTURED CONFIG (yaml/json/toml/ini/cfg) is dual-use and does NOT
    auto-qualify — it needs the operator's explicit opt-in
    (AGENT_ULTRA_SURGICAL_ALLOW_CONFIG=1) and must still clear the
    denylist + shape checks;
  * everything else is full-pipeline work.

The denylist is files whose CONTENT executes in CI/build/install/runtime
contexts regardless of extension (workflows, hook managers, task runners,
package manifests, tool configs). Matching is path-aware (exact basename /
basename prefix / path prefix / shape), never substring — a doc ABOUT
Dockerfiles is a doc, not a Dockerfile. Stdlib only.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SURGICAL_DOC_EXTS = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".adoc",
})
SURGICAL_STRUCTURED_CONFIG_EXTS = frozenset({
    ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
})
SURGICAL_ALLOWED_EXTENSIONS = (SURGICAL_DOC_EXTS
                               | SURGICAL_STRUCTURED_CONFIG_EXTS)
# dotfiles have no suffix in pathlib — check by basename instead
SURGICAL_ALLOWED_DOTFILES = frozenset({
    ".gitignore", ".editorconfig", ".gitattributes",
})

# diff budget: surgical is for SMALL inert edits; anything bigger is full
# pipeline work by definition
SURGICAL_MAX_DIFF_LINES = 100
SURGICAL_MAX_DIFF_BYTES = 256 * 1024


def _config_opt_in() -> bool:
    return os.environ.get("AGENT_ULTRA_SURGICAL_ALLOW_CONFIG", "0") == "1"


# Paths ALWAYS denied in surgical mode even when the extension is allowed —
# these execute code in CI/build/install/runtime contexts.
SURGICAL_DENIED_BASENAMES = frozenset({
    # CI/CD (many providers)
    ".gitlab-ci.yml", "azure-pipelines.yml", "jenkinsfile", ".drone.yml",
    ".travis.yml", "bitbucket-pipelines.yml", "appveyor.yml",
    ".appveyor.yml", "cloudbuild.yaml", "cloudbuild.yml",
    ".woodpecker.yml", ".woodpecker.yaml", "buildkite.yml", ".semaphore.yml",
    "wercker.yml", "codeship-services.yml", "codeship-steps.yml",
    # git-hook MANAGERS (execute arbitrary shell on git ops)
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
    "lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml",
    ".overcommit.yml", ".huskyrc", ".huskyrc.json", ".huskyrc.yml",
    # task runners / build DSLs
    "taskfile.yml", "taskfile.yaml", "justfile", ".justfile",
    "makefile", "gnumakefile", "rakefile", "snakefile", "cmakelists.txt",
    # IaC / deploy (run code on apply/deploy)
    "serverless.yml", "serverless.yaml", "netlify.toml", "fly.toml",
    "vercel.json", "now.json", "pulumi.yaml", "template.yaml",
    "samconfig.toml", "app.yaml", "cloudformation.yaml", "cloudformation.yml",
    # container/build/devcontainer
    "dockerfile", "containerfile", ".dockerignore", "procfile",
    "devcontainer.json", "compose.yaml", "compose.yml",
    "skaffold.yaml", "skaffold.yml", "wrangler.toml", "turbo.json",
    "nx.json", "firebase.json", "deno.json", "deno.jsonc", "pubspec.yaml",
    "supervisord.ini", "supervisord.conf",
    # task-runner / tooling ecosystems that execute shell
    "mise.toml", ".mise.toml", ".mise.local.toml", "dvc.yaml",
    "moon.yml", "bunfig.toml", "biome.json", "earthly.toml", "earthfile",
    "goreleaser.yml", "goreleaser.yaml", "release-please-config.json",
    # test-runner / tool configs the shape regex doesn't catch by name
    "pytest.ini", "nodemon.json", ".flake8", ".pylintrc",
    # more code-executing tool configs (opt-in path)
    "helmfile.yaml", "helmfile.yml", "chezmoi.toml", ".chezmoi.toml",
    "dagger.json", "invoke.yaml", "invoke.yml", "tasks.py", "fabfile.py",
    "helmfile.d",
    # package / dependency manifests (code execution on install)
    "package.json", "package-lock.json", "composer.json", "composer.lock",
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "noxfile.py",
    "requirements.txt", "pipfile", "pipfile.lock", "conanfile.txt",
    "conanfile.py", "meta.yaml", "environment.yml", "environment.yaml",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum",
    "gemfile", "gemfile.lock", "podfile", "cartfile",
    "pom.xml", "build.gradle", "build.gradle.kts", "build.sbt", "build.xml",
    "build.boot", "project.clj", "deps.edn", "mix.exs", "rebar.config",
    ".npmrc", ".yarnrc", ".yarnrc.yml", "yarn.lock", "pnpm-lock.yaml",
    "renovate.json", ".renovaterc", ".renovaterc.json", "dependabot.yml",
    # environment injection
    ".env", ".env.local", ".env.production", ".env.development",
    # editor/runtime autoloaders that execute
    ".vimrc", ".exrc", ".ideavimrc", "conftest.py",
})
SURGICAL_DENIED_BASENAME_PREFIXES = ("docker-compose", "compose.",
                                     "taskfile", ".env.", "cloudbuild.")
SURGICAL_DENIED_PATH_PREFIXES = (
    ".github/workflows/", ".github/actions/", ".circleci/",
    ".gitea/workflows/", ".forgejo/workflows/", ".woodpecker/",
    ".buildkite/", ".husky/", ".teamcity/", ".azuredevops/",
    ".devcontainer/", "k8s/", "helm/", "kustomize/", ".config/systemd/",
    ".vscode/", ".moon/")
# shape-based detection so novel/unlisted CI/hook/deploy tools are caught
# structurally (not by name). Any yml/yaml/json/toml whose path looks like
# a workflow/pipeline/CI/deploy/hook config falls back to the FULL
# pipeline. Doc-about-CI (.md/.txt) is unaffected — this only gates the
# executable-config extensions.
_EXEC_CONFIG_EXTS = frozenset(
    {".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".conf"})
_EXEC_PATH_RE = re.compile(
    r"(?:^|/)(?:\.?[a-z]*ci|workflows?|pipelines?|\.?deploy|hooks?|"
    r"actions?|compose|devcontainer|container|k8s|kube|helm[a-z]*|kustomiz|"
    r"serverless|terraform|ansible|chezmoi|dagger)(?:/|$|[._-])",
    re.IGNORECASE)
# the whole TOOL-CONFIG class (test runners, linters, transpilers,
# bundlers) executes arbitrary code when its tool runs — a name census
# never converges, so match the SHAPE: <tool>.config.<ext>,
# .<tool>rc[.<ext>], <tool>.conf.<ext>. Denied -> falls back to full Bob.
_TOOL_CONFIG_RE = re.compile(
    r"(?:\.config\.[a-z0-9]+$)"
    r"|(?:\.conf\.[a-z0-9]+$)"
    r"|(?:^\.[a-z][a-z0-9_-]*rc$)"
    r"|(?:^\.[a-z][a-z0-9_-]*rc\.[a-z0-9]+$)",
    re.IGNORECASE)
# enforcement infrastructure is NEVER surgical-editable — the gate cannot
# be reconfigured through its own lightweight lane. Matched as path
# segments.
SURGICAL_DENIED_PATH_SEGMENTS = (".git", ".agent-ultra")


def surgical_file_denied(path: str) -> bool:
    """True when *path* is on the surgical deny list — a file whose content
    executes in CI/build/install contexts regardless of extension.
    Path-aware matching (exact basename / basename prefix / path prefix /
    shape), NOT substring."""
    norm = os.path.normpath(str(path)).replace("\\", "/").lower()
    base = norm.rsplit("/", 1)[-1]
    if base in SURGICAL_DENIED_BASENAMES:
        return True
    if any(base.startswith(pfx) for pfx in SURGICAL_DENIED_BASENAME_PREFIXES):
        return True
    if _TOOL_CONFIG_RE.search(base):
        return True
    segments = set(norm.split("/"))
    if any(seg in segments for seg in SURGICAL_DENIED_PATH_SEGMENTS):
        return True
    if any(norm.startswith(pfx) or f"/{pfx}" in f"/{norm}"
           for pfx in SURGICAL_DENIED_PATH_PREFIXES):
        return True
    ext = ("." + base.rsplit(".", 1)[-1]) if "." in base else ""
    if ext in _EXEC_CONFIG_EXTS and _EXEC_PATH_RE.search(norm):
        return True
    return False


def surgical_ext_allowed(path: str) -> bool:
    """True when *path* auto-qualifies for the surgical lane: truly-inert
    PROSE and known-inert dotfiles always; STRUCTURED CONFIG only when the
    operator opts in (AGENT_ULTRA_SURGICAL_ALLOW_CONFIG=1) AND it clears
    the denylist/shape checks. Otherwise: full pipeline."""
    p = Path(str(path))
    if p.name.lower() in SURGICAL_ALLOWED_DOTFILES:
        return True
    ext = p.suffix.lower()
    if ext in SURGICAL_DOC_EXTS:
        return True
    if ext in SURGICAL_STRUCTURED_CONFIG_EXTS:
        return _config_opt_in() and not surgical_file_denied(path)
    return False


def staged_set_qualifies(staged_files, declared) -> bool:
    """True when every staged file independently qualifies for the surgical
    lane AND was declared up front. This is the C1 mode-downgrade defense's
    routing test: run.json's mode field is a REQUEST the gate verifies,
    never a trusted assertion — flipping a run's mode to surgical with code
    staged routes the commit to the FULL gate, which then demands the full
    receipt chain."""
    declared_norm = {os.path.normpath(str(d)).replace("\\", "/").lower()
                     for d in (declared or [])}
    for rel in staged_files or []:
        norm = os.path.normpath(str(rel)).replace("\\", "/").lower()
        if norm.startswith(".agent-ultra/"):
            continue                     # run machinery rides along
        if surgical_file_denied(rel) or not surgical_ext_allowed(rel):
            return False
        if norm not in declared_norm:
            return False
    return True


def _git_diff_stat(workspace: Path) -> "int | None":
    """Total staged lines changed (added+deleted). Fail-closed: None on any
    git error so the diff-budget check blocks rather than skips. Returns -1
    if any staged file is BINARY (numstat '-') — surgical is text-only."""
    try:
        p = subprocess.run(["git", "-C", str(workspace), "diff", "--cached",
                            "--numstat"], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    total = 0
    for ln in p.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            if parts[0] == "-" or parts[1] == "-":
                return -1                # binary staged edit
            for n in parts[:2]:
                if n.isdigit():
                    total += int(n)
    return total


def _git_diff_bytes(workspace: Path) -> "int | None":
    """Total bytes in the staged diff. Fail-closed: None on git error."""
    try:
        p = subprocess.run(["git", "-C", str(workspace), "diff", "--cached"],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return len(p.stdout or b"")


def diff_budget_error(workspace: Path) -> "str | None":
    """None if the staged diff fits the surgical budget; else the blocking
    message. Fail-closed on any git error, over-line, binary, or over-byte."""
    lines = _git_diff_stat(Path(workspace))
    if lines is None:
        return ("surgical: could not compute staged diff size (git error) "
                "— fail-closed")
    if lines == -1:
        return "surgical: staged diff contains a BINARY file — use full mode"
    if lines > SURGICAL_MAX_DIFF_LINES:
        return (f"surgical: staged diff {lines} lines exceeds the "
                f"{SURGICAL_MAX_DIFF_LINES}-line budget — use full mode")
    nbytes = _git_diff_bytes(Path(workspace))
    if nbytes is None:
        return ("surgical: could not measure staged diff bytes (git error) "
                "— fail-closed")
    if nbytes > SURGICAL_MAX_DIFF_BYTES:
        return (f"surgical: staged diff {nbytes} bytes exceeds the "
                f"{SURGICAL_MAX_DIFF_BYTES}-byte budget — use full mode")
    return None
