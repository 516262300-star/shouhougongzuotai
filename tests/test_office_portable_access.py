"""Reject unsafe bootstrap inputs without installing services or opening a listener."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "office-portable-access.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell required")


def invoke(*args):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         *args, "-WhatIf"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )


@pytest.mark.parametrize("address", ["0.0.0.0", "8.8.8.8", "192.168.3.0/24", "::1", "localhost"])
def test_rejects_non_private_or_non_single_ipv4(address):
    result = invoke("-DevelopmentAddress", address)
    assert result.returncode != 0
    assert "IPv4" in result.stderr


def test_rejects_private_key_input(tmp_path):
    key = tmp_path / "key.pub"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    result = invoke("-DevelopmentAddress", "192.168.3.95", "-PublicKeyFile", str(key))
    assert result.returncode != 0
    assert "public key required" in result.stderr


def test_tampered_archive_rejected_even_in_whatif(tmp_path):
    import base64

    key = tmp_path / "key.pub"
    public = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"a" * 32
    key.write_text("ssh-ed25519 " + base64.b64encode(public).decode())
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"untrusted archive")
    result = invoke("-DevelopmentAddress", "192.168.3.95", "-PublicKeyFile", str(key),
                    "-Archive", str(archive))
    assert result.returncode != 0
    assert "SHA256 mismatch" in result.stderr


def test_stop_whatif_needs_no_admin_or_managed_state():
    result = invoke("-Action", "Stop")
    assert result.returncode == 0
    assert "22222" in result.stdout
