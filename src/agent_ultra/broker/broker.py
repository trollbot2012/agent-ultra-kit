"""Command broker — risk classification + ledger for host command execution.

The security tension (model output should not directly execute arbitrary host
commands) is resolved NOT by forbidding host execution, but by routing every
model-authored command through this broker, which:

  1. classifies the command into a RISK TIER,
  2. records a ledger entry (command, cwd, reason, expected effect, tier,
     output, exit code),
  3. lets tiers in ``auto_run_tiers`` execute, while everything else must
     pass an approval gate or run in a sandbox — never silently dropped,
     never silently auto-run.

Two operating modes are just different ``auto_run_tiers``:
  - TRUSTED-OWNER (the agent's own execution): {SAFE, ELEVATED} — tests,
    builds, writes, installs, service restarts auto-run; only DANGEROUS gates.
  - CRITIC (auto-executing critic-PROPOSED checks): {SAFE} only — reads and
    inspection auto-run; anything with side effects is parked for approval.

Risk tiers:
  SAFE       read files, list dirs, grep/search, read-only git queries.
  ELEVATED   tests/builds, file writes, installs, moves/copies, service
             restarts, git working-tree mutations. Arbitrary code execution,
             but normal local dev.
  DANGEROUS  delete files, touch credentials/secrets, change live infra,
             publish/deploy, spend money, irreversible git history, disable
             security controls, pipe remote content into a shell.

HARD RULE (safe default): a DANGEROUS command with NO approval path (no
approver callable, no sandbox) is DENIED, loudly, in the ledger. Parking it
as "requires_approval" when nothing can ever approve it is a silent drop.
The ``allow_dangerous_without_approval=True`` override downgrades that denial
back to a parked "requires_approval" entry; it NEVER auto-runs the command.

This is a RISK ROUTER, not a security sandbox. Genuinely untrusted or
adversarial commands belong in a real sandbox (see the optional Docker
adapter). Ledger output is passed through secret redaction before being
written; the command line itself is stored verbatim for audit exactness, so
do not put secrets on command lines.

Stdlib only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ..evidence.reader import redact_secrets

SAFE = "safe"
ELEVATED = "elevated"
DANGEROUS = "dangerous"

TRUSTED_OWNER_TIERS = frozenset({SAFE, ELEVATED})
CRITIC_TIERS = frozenset({SAFE})

# -- DANGEROUS: require approval/sandbox even for the trusted owner ----------
# PowerShell and POSIX spellings side by side so one classifier covers both
# host shells.
_DANGEROUS_RES = [
    (re.compile(r"(?i)\b(remove-item|rm|del|erase|rmdir|rd|ri|rimraf|shred)\b"
                r"|\bRemove-\w+|\bdel\s+/|-recurse\s+-force"), "deletes files"),
    (re.compile(r"(?i)\bformat-volume\b|\bformat\s+[a-z]:|\bmkfs\b"),
     "formats a volume"),
    (re.compile(r"(?i)\.env\b|\bsecret\b|\bapi[_-]?key\b|\bpassword\b|"
                r"\bSet-Secret\b|\bcredential\b"),
     "touches credentials/secrets"),
    (re.compile(r"(?i)\b(kubectl|helm|terraform\s+(apply|destroy)|docker\s+push|"
                r"az\b|aws\b|gcloud\b)\b|\bdeploy\b|\bpublish\b"),
     "changes live infrastructure / deploys"),
    (re.compile(r"(?i)\bgit\s+push\b.*(--force|-f\b|--delete|\s:)|"
                r"\bgit\s+reset\s+--hard|\bfilter-branch\b|\bfilter-repo\b|"
                r"\breflog\s+expire|\bgit\s+gc\s+--prune|\bgit\s+branch\s+-D\b"),
     "irreversible git history op"),
    (re.compile(r"(?i)\|\s*(sudo\s+)?(ba)?sh\b|\bcurl\b[^|]*\|\s*(ba)?sh|"
                r"\bwget\b[^|]*\|\s*(ba)?sh|\|\s*iex\b|\biex\s*\("),
     "pipes remote content into a shell"),
    (re.compile(r"(?i)\bSet-ExecutionPolicy\b|\bSet-MpPreference\b|"
                r"\bdisable-\w*(defender|firewall|security)|"
                r"\bnetsh\s+advfirewall|\bufw\s+disable\b|\bsetenforce\s+0\b"),
     "disables a security control"),
    (re.compile(r"(?i)\breg\s+delete\b|\bRemove-ItemProperty\b|"
                r"\bClear-\w+|\bStop-Computer\b|\bRestart-Computer\b"),
     "destructive system/registry op"),
    (re.compile(r"(?i)\bstripe\b|\bbilling\b|\bcharge\b|\bpurchase\b"),
     "may spend money"),
    # bob's operator-only escape flags (abandon an unproven run /
    # force-supersede one / start unbounded). Operator decisions,
    # never an agent's -- an agent-driven shell must not auto-run them.
    (re.compile(r"(?i)--operator-(abandon|force|unbounded)"),
     "operator-only pipeline escape flag"),
]

# -- ELEVATED: normal local dev with side effects ---------------------------
_ELEVATED_RES = [
    (re.compile(r"(?i)\b(set-content|out-file|add-content|new-item|tee-object|"
                r"tee|mkdir|touch|ln)\b|>>|(?<![0-9])>"), "writes a file"),
    (re.compile(r"(?i)\b(pip|pip3|uv|npm|pnpm|yarn|choco|winget|apt|apt-get|"
                r"brew|dotnet\s+add|cargo\s+add|go\s+get)\s+(install|add|i|"
                r"update|upgrade|sync)\b"), "installs/updates packages"),
    (re.compile(r"(?i)\b(restart-service|stop-service|start-service|"
                r"net\s+(start|stop)|sc\s+(start|stop|config)|"
                r"systemctl\s+(start|stop|restart|reload|enable|disable)|"
                r"service\s+\S+\s+(start|stop|restart|reload))\b|"
                r"\bStart-Process\b"), "controls a service/process"),
    (re.compile(r"(?i)\b(move-item|copy-item|mv|cp|move|copy|robocopy|xcopy|"
                r"rsync)\b"), "moves/copies files"),
    (re.compile(r"(?i)\bgit\s+(commit|checkout|switch|add|stash|merge|rebase|"
                r"pull|fetch|clean|apply|cherry-pick|revert|tag|push)\b"),
     "mutates the git working tree"),
    (re.compile(r"(?i)\b(pytest|tox|unittest|nose2|jest|mocha|vitest)\b|"
                r"\bnpm\s+(test|run|build|ci)\b|\b(make|ninja|msbuild)\b|"
                r"\bdotnet\s+(test|build|run)\b|\bcargo\s+(test|build|run)\b|"
                r"\bgo\s+(test|build|run)\b|\bpython\s+-m\s+pytest"),
     "runs tests/builds (arbitrary code)"),
    (re.compile(r"(?i)\b(set-itemproperty|rename-item|chmod|chown)\b"),
     "modifies an item"),
    (re.compile(r"(?i)\b(tar\s+(-[^\s]*x|--extract)|unzip|7z\s+x)\b"),
     "extracts an archive"),
]

# git subcommands that are pure reads even though 'git' isn't read-only.
_SAFE_GIT_RE = re.compile(
    r"(?i)^git\s+(status|diff|log|show|remote(\s+-v)?|rev-parse|branch\s*$|"
    r"branch\s+-[alr]|ls-files|blame|cat-file|describe|shortlog|"
    r"config\s+--get)")

# Built-in pure-read test: every pipeline segment starts with a known
# read-only program, and the command has no write/exec shell syntax.
# Deliberately conservative — unknowns fall through to ELEVATED.
_READ_ONLY_HEADS = frozenset({
    "ls", "dir", "cat", "head", "tail", "less", "more", "grep", "egrep",
    "fgrep", "rg", "find", "fd", "which", "where", "whereis", "type", "file",
    "stat", "du", "df", "wc", "pwd", "whoami", "id", "uname", "hostname",
    "date", "ps", "printenv", "echo", "printf", "sort", "uniq", "cut",
    "tr", "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "tree", "realpath",
    "readlink", "basename", "dirname", "test", "true", "false", "column",
    "select-string", "test-path", "measure-object", "format-list",
    "format-table", "format-wide", "out-string", "write-output", "write-host",
})
_WRITE_EXEC_SYNTAX_RE = re.compile(r"[;&<>`\n]|\$\(|>>")
# Flags that give an otherwise read-only program write or exec powers.
_READ_ONLY_ESCAPE_RE = re.compile(
    r"(?i)\s(-exec(dir)?|--exec|-delete|-ok|-x|-X)\b"
    r"|\bsort\b[^|]*\s-o\b|\bdate\b[^|]*\s-s\b")


def _default_is_read_only(cmd: str) -> bool:
    if _WRITE_EXEC_SYNTAX_RE.search(cmd) or _READ_ONLY_ESCAPE_RE.search(cmd):
        return False
    for segment in cmd.split("|"):
        words = segment.strip().split()
        if not words:
            return False
        head = words[0].lower()
        # PowerShell Get-* verbs are read-only by convention; Get-Credential
        # is caught by the DANGEROUS credentials pattern before this runs.
        if head not in _READ_ONLY_HEADS and not head.startswith("get-"):
            return False
    return True


def classify(cmd: str, is_read_only=None) -> tuple[str, str]:
    """Return (tier, reason). DANGEROUS wins over ELEVATED; pure reads and
    safe-git are SAFE; unknowns default ELEVATED (auto in owner mode, gated
    in critic mode). ``is_read_only``: optional stricter pure-read callable."""
    c = (cmd or "").strip()
    if not c:
        return SAFE, "empty"
    for rx, why in _DANGEROUS_RES:
        if rx.search(c):
            return DANGEROUS, why
    read_only_test = is_read_only if is_read_only is not None else _default_is_read_only
    try:
        if read_only_test(c):
            return SAFE, "pure read (no side effects, no eval)"
    except Exception:
        pass  # a broken injected test must not break classification
    if _SAFE_GIT_RE.match(c):
        return SAFE, "read-only git query"
    for rx, why in _ELEVATED_RES:
        if rx.search(c):
            return ELEVATED, why
    return ELEVATED, "unrecognized command (defaulted to elevated)"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class BrokerResult:
    command: str
    cwd: str
    reason: str
    expected_effect: str
    risk_tier: str
    tier_reason: str
    status: str = ""  # passed|failed|timeout|error|requires_approval|rejected|denied
    exit_code: int | None = None
    output: str = ""
    backend: str = "host"  # host | sandbox
    ts: str = ""

    @property
    def ran(self) -> bool:
        return self.status in ("passed", "failed", "timeout", "error")


_record_lock = threading.Lock()


def record(res: BrokerResult, ledger_path: Path | str | None) -> None:
    """Append *res* to the JSONL ledger. Best-effort; never raises."""
    if not ledger_path:
        return
    if not res.ts:
        res.ts = _utc_now()
    try:
        path = Path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(res), ensure_ascii=False) + "\n"
        with _record_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except (OSError, TypeError, ValueError):
        pass  # the ledger must never break execution


def _default_shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-c", command]


class CommandBroker:
    """Routes model-authored commands through risk classification + a ledger.

    auto_run_tiers: which tiers execute without approval.
        TRUSTED_OWNER_TIERS ({safe, elevated}) or CRITIC_TIERS ({safe}).
    approver: optional callable(BrokerResult) -> bool consulted for a command
        whose tier is NOT in auto_run_tiers.
    sandbox_argv: optional callable(command) -> argv. A non-approved command
        runs THERE instead of being parked ("approval or sandbox").
    allow_dangerous_without_approval: downgrade the hard DENY (dangerous tier,
        no approval path) to a parked requires_approval entry. Never auto-runs.
    """

    def __init__(self, ledger_path: Path | str | None = None,
                 auto_run_tiers=CRITIC_TIERS, timeout: int = 60,
                 is_read_only=None, approver=None, sandbox_argv=None,
                 allow_dangerous_without_approval: bool = False,
                 shell_argv=None, now=None):
        self.ledger_path = ledger_path
        self.auto_run_tiers = frozenset(auto_run_tiers)
        self.timeout = timeout
        self.is_read_only = is_read_only
        self.approver = approver
        self.sandbox_argv = sandbox_argv
        self.allow_dangerous_without_approval = allow_dangerous_without_approval
        self._shell_argv = shell_argv or _default_shell_argv
        self._now = now or _utc_now

    def classify(self, command: str) -> tuple[str, str]:
        return classify(command, is_read_only=self.is_read_only)

    def run(self, command: str, cwd: str = "", reason: str = "",
            expected_effect: str = "") -> BrokerResult:
        tier, why = self.classify(command)
        res = BrokerResult(command=command, cwd=cwd or "", reason=reason,
                           expected_effect=expected_effect, risk_tier=tier,
                           tier_reason=why, ts=self._now())

        approved = tier in self.auto_run_tiers
        asked = False
        if not approved and self.approver is not None:
            asked = True
            try:
                approved = bool(self.approver(res))
            except Exception:
                approved = False

        if approved:
            self._execute(res, self._shell_argv)
        elif asked:
            res.status = "rejected"
            res.output = f"NOT RUN - approver rejected ({tier}: {why})."
        elif self.sandbox_argv is not None:
            res.backend = "sandbox"
            self._execute(res, self.sandbox_argv)
        elif tier == DANGEROUS and not self.allow_dangerous_without_approval:
            res.status = "denied"
            res.output = ("DENIED - dangerous tier with NO approval path "
                          f"({why}). Configure an approver or sandbox, or set "
                          "allow_dangerous_without_approval to park instead.")
        else:
            res.status = "requires_approval"
            res.output = (f"NOT RUN - {tier} tier ({why}); parked for "
                          "approval or sandbox.")
        res.output = redact_secrets(res.output)
        record(res, self.ledger_path)
        return res

    def _execute(self, res: BrokerResult, argv_fn) -> None:
        try:
            proc = subprocess.run(
                argv_fn(res.command), cwd=res.cwd or None,
                capture_output=True, text=True, timeout=self.timeout)
            res.output = ((proc.stdout or "") + (proc.stderr or ""))[:4000]
            res.exit_code = proc.returncode
            res.status = "passed" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            res.status = "timeout"
            res.output = f"(no result within {self.timeout}s)"
        except Exception as e:
            res.status = "error"
            res.output = f"{e.__class__.__name__}: {e}"
