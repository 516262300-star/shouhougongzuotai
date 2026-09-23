"""Bootstrap guards and opt-in loopback regression checks; never open LAN access."""
import json
import os
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


@pytest.mark.parametrize("scenario", ["accepted", "completed", "occupied"])
def test_real_debug_listener_startup_race(tmp_path, scenario):
    """A fast accepted/completed session must not look like a bind failure.

    Set LDS_TEST_OPENSSH_DIR to an independently verified Windows OpenSSH package.
    Tests use throwaway host keys, loopback only, and no firewall/service changes.
    """
    directory = os.environ.get("LDS_TEST_OPENSSH_DIR")
    if not directory:
        pytest.skip("Verified Windows OpenSSH package required for integration checks")
    binary = Path(directory).resolve() / "sshd.exe"
    keygen = Path(directory).resolve() / "ssh-keygen.exe"
    assert binary.is_file() and keygen.is_file()
    key = tmp_path / "host_ed25519"
    subprocess.run([str(keygen), "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   check=True, capture_output=True, timeout=10)
    # Load just the actual readiness function, avoiding privileged bootstrap side effects.
    harness = tmp_path / "check.ps1"
    harness.write_text(r'''
param($Source, $Binary, $Directory, $Scenario)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Source,[ref]$null,[ref]$null)
$function = $ast.Find({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Wait-PortableListenerStartup'
},$false)
. ([ScriptBlock]::Create($function.Extent.Text))
$reserve = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
$reserve.Start()
$port = $reserve.LocalEndpoint.Port
if ($Scenario -ne 'occupied') { $reserve.Stop() }
$config = Join-Path $Directory 'sshd_config'
[IO.File]::WriteAllLines($config, @(
    "Port $port", 'AddressFamily inet', 'ListenAddress 127.0.0.1',
    ('HostKey "' + (Join-Path $Directory 'host_ed25519').Replace('\','/') + '"'),
    'PasswordAuthentication no', 'KbdInteractiveAuthentication no',
    'AuthenticationMethods publickey', 'DisableForwarding yes', 'PermitTTY no',
    'LoginGraceTime 30'
), [Text.UTF8Encoding]::new($false))
$errLog = Join-Path $Directory 'listener.err.log'
$outLog = Join-Path $Directory 'listener.out.log'
$process = $null
$client = $null
try {
    $process = Start-Process -FilePath $Binary -ArgumentList @('-d','-e','-f',('"'+$config+'"')) `
        -WindowStyle Hidden -RedirectStandardError $errLog -RedirectStandardOutput $outLog -PassThru
    if ($Scenario -eq 'occupied') {
        if (-not $process.WaitForExit(5000)) { throw 'Expected real bind failure' }
    } else {
        $limit = (Get-Date).AddSeconds(8)
        do {
            $client = [Net.Sockets.TcpClient]::new()
            try { $client.Connect('127.0.0.1',$port); break }
            catch { $client.Dispose(); $client=$null; Start-Sleep -Milliseconds 30 }
        } while ((Get-Date) -lt $limit -and -not $process.HasExited)
        if (-not $client) { throw 'Loopback connection failed' }
        $stream = $client.GetStream()
        $stream.ReadTimeout = 3000
        $bytes = New-Object byte[] 1024
        $count = $stream.Read($bytes,0,$bytes.Length)
        if ([Text.Encoding]::ASCII.GetString($bytes,0,$count) -notmatch '^SSH-2.0-') {
            throw 'No SSH banner'
        }
        # Deliberately finish accept BEFORE the launcher first checks readiness.
        if ($Scenario -eq 'completed') {
            $client.Dispose(); $client=$null
            if (-not $process.WaitForExit(5000)) { throw 'Expected completed session' }
        }
    }
    $sockets = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    $socketCount = @($sockets |
        Where-Object OwningProcess -eq $process.Id).Count
    $ready = Wait-PortableListenerStartup $process $errLog $port
    @{scenario=$Scenario;ready=$ready;listen_count=$socketCount;alive=(-not $process.HasExited)} |
        ConvertTo-Json -Compress
} finally {
    if ($client) { $client.Dispose() }
    $reserve.Stop()
    if ($process) {
        if (-not $process.HasExited) { $process.Kill(); $null=$process.WaitForExit(3000) }
        $process.Dispose()
    }
}
''', encoding="utf-8-sig")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness),
         str(SCRIPT), str(binary), str(tmp_path), scenario],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    server_log = (tmp_path / "listener.err.log").read_text(errors="replace")
    if scenario == "occupied":
        assert "Bind to port" in server_log and "failed" in server_log
    assert observed["listen_count"] == 0
    assert observed["ready"] is (scenario != "occupied")
    assert observed["alive"] is (scenario == "accepted")
