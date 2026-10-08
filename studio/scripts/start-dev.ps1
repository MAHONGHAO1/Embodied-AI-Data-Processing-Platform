# QuicData start for Windows

# Usage: powershell -ExecutionPolicy Bypass -File scripts/start-dev.ps1



$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$VenvDir = Join-Path $Root "backend\.venv"



if (-not (Test-Path $VenvDir)) {

    Write-Host "ERROR: Run scripts/setup.ps1 first" -ForegroundColor Red

    exit 1

}



$Port = 8000

$LanIps = @(
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike '127.*' -and
        $_.IPAddress -notlike '169.254.*' -and
        $_.PrefixOrigin -ne 'WellKnown'
    } |
    Select-Object -ExpandProperty IPAddress
)
$PrimaryLan = ($LanIps | Where-Object { $_ -eq '192.168.112.161' } | Select-Object -First 1)
if (-not $PrimaryLan) { $PrimaryLan = $LanIps | Select-Object -First 1 }

Write-Host "=== QuicData MVP ===" -ForegroundColor Cyan
Write-Host "Listen:   0.0.0.0:$Port (LAN accessible)" -ForegroundColor White
Write-Host "Local:    http://127.0.0.1:$Port" -ForegroundColor White
if ($PrimaryLan) {
    Write-Host "LAN:      http://${PrimaryLan}:$Port" -ForegroundColor Green
    Write-Host "Client:   http://${PrimaryLan}:$Port  (e.g. 192.168.112.115)" -ForegroundColor Green
}
Write-Host "API Docs: http://127.0.0.1:$Port/docs" -ForegroundColor White
Write-Host "Firewall: scripts/open-lan-firewall.ps1 (Admin, if LAN blocked)" -ForegroundColor Gray

Write-Host "Login: admin@quicdata.com / admin123" -ForegroundColor Yellow

Write-Host "Press Ctrl+C to stop" -ForegroundColor Gray

Write-Host ""



$Activate = Join-Path $VenvDir "Scripts\Activate.ps1"

. $Activate

Set-Location (Join-Path $Root "backend")

python run.py

