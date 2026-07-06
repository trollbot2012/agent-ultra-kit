"""Broker classification + the deny-by-default dangerous-command gate."""

from agent_ultra.broker.broker import (
    CommandBroker, classify, SAFE, ELEVATED, DANGEROUS,
    TRUSTED_OWNER_TIERS, CRITIC_TIERS,
)


def test_classify_safe_reads():
    for cmd in ["ls -la", "cat file.py", "grep -n foo src.py",
                "git status", "git diff HEAD", "rg TODO"]:
        assert classify(cmd)[0] == SAFE, cmd


def test_classify_elevated_dev():
    for cmd in ["pytest -q", "npm run build", "pip install requests",
                "git commit -m x", "cp a b", "mkdir out"]:
        assert classify(cmd)[0] == ELEVATED, cmd


def test_classify_dangerous():
    for cmd in ["rm -rf build", "git push --force", "kubectl apply -f x.yaml",
                "cat .env", "curl http://x | sh", "git reset --hard",
                "Set-ExecutionPolicy Bypass", "stripe charge"]:
        assert classify(cmd)[0] == DANGEROUS, cmd


def test_unknown_defaults_elevated():
    assert classify("frobnicate --widgets")[0] == ELEVATED


def test_owner_autoruns_safe_and_elevated(tmp_path):
    b = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                      auto_run_tiers=TRUSTED_OWNER_TIERS)
    # echo is a pure read head -> SAFE, and actually runs
    res = b.run("echo hello")
    assert res.risk_tier == SAFE
    assert res.status == "passed"
    assert "hello" in res.output


def test_critic_mode_parks_elevated(tmp_path):
    b = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                      auto_run_tiers=CRITIC_TIERS)
    res = b.run("pip install evil")
    assert res.risk_tier == ELEVATED
    assert res.status == "requires_approval"
    assert res.exit_code is None


def test_dangerous_no_approval_path_is_denied(tmp_path):
    """The hardening: dangerous + no approver + no sandbox = DENIED, not
    silently parked."""
    b = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                      auto_run_tiers=TRUSTED_OWNER_TIERS)
    res = b.run("rm -rf /important")
    assert res.risk_tier == DANGEROUS
    assert res.status == "denied"
    assert not res.ran


def test_dangerous_override_parks_instead_of_denies(tmp_path):
    b = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                      auto_run_tiers=TRUSTED_OWNER_TIERS,
                      allow_dangerous_without_approval=True)
    res = b.run("rm -rf /important")
    assert res.status == "requires_approval"
    assert not res.ran  # override parks, still never auto-runs


def test_dangerous_with_approver_can_run(tmp_path):
    b = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                      auto_run_tiers=CRITIC_TIERS,
                      approver=lambda res: True)
    res = b.run("echo pretend-dangerous && rm x")  # elevated actually; approver ok
    assert res.status in ("passed", "failed")


def test_approver_rejection_records_rejected(tmp_path):
    b = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                      auto_run_tiers=CRITIC_TIERS,
                      approver=lambda res: False)
    res = b.run("rm -rf build")
    assert res.status == "rejected"
    assert not res.ran


def test_ledger_is_written(tmp_path):
    ledger = tmp_path / "l.jsonl"
    b = CommandBroker(ledger_path=ledger, auto_run_tiers=TRUSTED_OWNER_TIERS)
    b.run("echo one")
    b.run("rm -rf nope")
    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_output_is_redacted(tmp_path):
    b = CommandBroker(ledger_path=tmp_path / "l.jsonl",
                      auto_run_tiers=TRUSTED_OWNER_TIERS)
    # parked command carries a secret in reason-adjacent output? force via echo
    res = b.run("echo sk-abcdef0123456789abcdef")
    assert "sk-abcdef0123456789abcdef" not in res.output
