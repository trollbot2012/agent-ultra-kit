"""Evidence reader — gathering, low-context detection, secret redaction."""

from agent_ultra.evidence.reader import (
    gather, redact_secrets, is_low_context, Evidence,
)


def test_redacts_openai_key():
    out = redact_secrets("key = sk-abcdefghijklmnop0123456789")
    assert "sk-abcdefghij" not in out
    assert "REDACTED" in out


def test_redacts_github_token():
    out = redact_secrets("token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    assert "ghp_ABCDEF" not in out


def test_redacts_kv_secret_keeps_key_name():
    out = redact_secrets('password = "hunter2hunter2"')
    assert "password" in out
    assert "hunter2" not in out


def test_redacts_private_key_block():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----")
    assert "MIIabc" not in redact_secrets(pem)


def test_low_context_detection():
    assert is_low_context("short")
    assert not is_low_context("x" * 600)


def test_gather_reads_source(tmp_path):
    (tmp_path / "a.py").write_text("def f(): return 1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("notes\n", encoding="utf-8")
    (tmp_path / "img.png").write_bytes(b"\x00\x01\x02binary")
    ev = gather(dirs=[str(tmp_path)])
    assert "def f()" in ev.text
    assert len(ev.files) >= 1
    assert not any("img.png" in f for f in ev.files)  # binary skipped


def test_gather_redacts_secrets_in_files(tmp_path):
    (tmp_path / "cfg.py").write_text(
        "API_KEY = 'sk-verysecret0123456789abcd'\n", encoding="utf-8")
    ev = gather(dirs=[str(tmp_path)])
    assert "sk-verysecret" not in ev.text


def test_gather_skips_node_modules(tmp_path):
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("bad\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("ok\n", encoding="utf-8")
    ev = gather(dirs=[str(tmp_path)])
    # match on basename: the tmp dir name itself can contain "node_modules"
    assert not any(f.replace("\\", "/").endswith("/index.js") for f in ev.files)
    assert any(f.replace("\\", "/").endswith("/good.py") for f in ev.files)
