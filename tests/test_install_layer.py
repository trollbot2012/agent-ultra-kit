"""doctor / init / demo / config — the install-and-verify layer."""

import os
from pathlib import Path

from agent_ultra.adapters.doctor import (
    load_simple_yaml, effective_config, panel_demo, broker_demo, ultra_demo,
)
from agent_ultra.adapters.cli import main as cli_main


def test_load_simple_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("# comment\nbase_url: http://x/v1\nroutes: a, b\n\nmode: mixed\n",
                 encoding="utf-8")
    cfg = load_simple_yaml(p)
    assert cfg["base_url"] == "http://x/v1"
    assert cfg["routes"] == "a, b"
    assert cfg["mode"] == "mixed"


def test_effective_config_env_overrides(tmp_path, monkeypatch):
    p = tmp_path / "agent-ultra.yaml"
    p.write_text("routes: from-file\nbase_url: http://file/v1\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_ULTRA_ROUTES", "from-env")
    cfg = effective_config(str(p))
    assert cfg["routes"] == ["from-env"]          # env wins
    assert cfg["base_url"] == "http://file/v1"    # file used where no env


def test_effective_config_defaults(monkeypatch):
    for var in ("AGENT_ULTRA_ROUTES", "AGENT_ULTRA_BASE_URL",
                "AGENT_ULTRA_API_KEY_ENV", "AGENT_ULTRA_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    cfg = effective_config("definitely-missing.yaml")
    assert cfg["routes"] == ["gpt-4o-mini"]
    assert cfg["api_key_env"] == "OPENAI_API_KEY"


def test_panel_demo_offline(tmp_path):
    ok, report = panel_demo(tmp_path)
    assert ok and report.accepted


def test_broker_demo_denies_dangerous(tmp_path):
    ok, detail = broker_demo(tmp_path)
    assert ok
    assert detail["dangerous"] == "denied"


def test_ultra_demo_offline(tmp_path):
    ok, rep = ultra_demo(tmp_path)
    assert ok
    assert rep.panel_ran


def test_cli_doctor_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli_main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 fail" in out


def test_cli_demo_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli_main(["demo"])
    assert rc == 0
    assert "DEMO PASSED" in capsys.readouterr().out


def test_cli_init_scaffolds(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli_main(["init"])
    assert rc == 0
    assert (tmp_path / "agent-ultra.yaml").exists()
    assert (tmp_path / ".env.example").exists()
    assert (tmp_path / "panel-runs").is_dir()
    # idempotent: never clobbers without --force
    (tmp_path / "agent-ultra.yaml").write_text("custom: yes", encoding="utf-8")
    cli_main(["init"])
    assert (tmp_path / "agent-ultra.yaml").read_text(encoding="utf-8") == "custom: yes"


def test_cli_mock_panel(capsys):
    rc = cli_main(["--mock", "panel", "Is this auth safe?",
                   "--lenses", "security"])
    assert rc == 0
    assert "DECISION" in capsys.readouterr().out
