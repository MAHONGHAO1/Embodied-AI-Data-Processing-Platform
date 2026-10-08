# Start Celery worker for preprocess / export tasks
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start-worker.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvDir = Join-Path $Root "backend\.venv"

if (-not (Test-Path $VenvDir)) {
    Write-Host "ERROR: Run scripts/setup.ps1 first" -ForegroundColor Red
    exit 1
}

Write-Host "=== QuicData Celery Worker ===" -ForegroundColor Cyan
Write-Host "Broker: Redis DB 1 (see backend/.env)" -ForegroundColor Gray

$Activate = Join-Path $VenvDir "Scripts\Activate.ps1"
. $Activate
Set-Location (Join-Path $Root "backend")
celery -A data.celery_app:celery_app worker --loglevel=info -Q quicdata
