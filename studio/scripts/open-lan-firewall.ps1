# Open inbound TCP 8000 for QuicData (requires administrator privileges)
# Usage: powershell -ExecutionPolicy Bypass -File scripts/open-lan-firewall.ps1

$RuleName = "QuicData API 8000"
$Port = 8000

$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Firewall rule already exists: $RuleName" -ForegroundColor Yellow
    exit 0
}

try {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -Profile Any `
        -Description "Allow LAN access to QuicData FastAPI on port 8000" | Out-Null
    Write-Host "Firewall rule created: TCP $Port inbound allowed" -ForegroundColor Green
    exit 0
} catch {
    Write-Host "Failed to create firewall rule (run PowerShell as Administrator): $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
