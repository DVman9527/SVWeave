"""SEC-04: redaction of host paths and secrets in log lines / evidence."""

from __future__ import annotations

from pathlib import Path

from svweave.security.redaction import REDACTED, REDACTED_PATH, Redactor


def test_secrets_are_redacted_everywhere() -> None:
    r = Redactor()
    cases = [
        "API_KEY=abc123def456",
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCY",
        "token: ghp_0123456789abcdefghij0123456789abcdef",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "password = 'hunter2 hunter2'",
        "key AKIAABCDEFGHIJKLMNOP leaked",
        "clone https://user:pa55w0rd@git.example.com/x.git",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJB\n-----END RSA PRIVATE KEY-----",
    ]
    for c in cases:
        out = r.text(c)
        for needle in ("abc123def456", "wJalrXUtnF", "ghp_0123", "eyJhbGci", "hunter2", "AKIAABCDEF",
                       "pa55w0rd", "MIIBOgIB"):
            assert needle not in out, (c, out)
        assert REDACTED in out


def test_paths_outside_allowed_roots_are_redacted(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    r = Redactor([ws])
    line = f"index {ws}/snap/a.sv failed; also /home/alice/customer_ip/rtl/top.sv and /opt/vendor/lib/x.so"
    out = r.text(line)
    assert f"{ws}/snap/a.sv" in out
    assert "/home/alice" not in out and "customer_ip" not in out and "/opt/vendor" not in out
    assert out.count(REDACTED_PATH) == 2


def test_system_paths_and_relative_paths_are_kept() -> None:
    r = Redactor()
    s = "loading /usr/lib/x86_64-linux-gnu/libc.so.6 from uvm/pkg/a.sv and /proc/self/status and http://h/x"
    assert r.text(s) == s


def test_prefix_lookalike_is_not_allowed(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    r = Redactor([ws])
    assert REDACTED_PATH in r.text(f"{ws}-evil/secret/file.sv")


def test_value_redacts_nested_structures_and_secret_keys() -> None:
    r = Redactor(["/ws"])
    obj = {"msg": "at /home/bob/private/x.sv", "api_key": "sekret", "nested": [{"token": "t", "ok": "/ws/a/b.sv"}],
           "n": 3}
    out = r.value(obj)
    assert out["api_key"] == REDACTED and out["nested"][0]["token"] == REDACTED
    assert out["nested"][0]["ok"] == "/ws/a/b.sv" and out["n"] == 3
    assert "bob" not in out["msg"]


def test_tail_and_file_redaction(tmp_path: Path) -> None:
    raw = tmp_path / "raw.log"
    raw.write_text("start\nSECRET_TOKEN=zzz999\nat /home/eve/ip/top.sv\n")
    r = Redactor([tmp_path])
    t = r.tail_file(raw, 4096)
    assert "zzz999" not in t and "eve" not in t
    out = tmp_path / "out" / "clean.log"
    r.redact_file(raw, out)
    assert "zzz999" not in out.read_text() and oct(out.stat().st_mode & 0o777) == "0o600"
    assert r.tail_file(tmp_path / "missing.log") == ""
