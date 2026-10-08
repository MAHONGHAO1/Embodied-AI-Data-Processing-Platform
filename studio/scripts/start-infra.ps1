# Start PostgreSQL + Redis via Docker Compose
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start-infra.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Docker not found. Install Docker Desktop or start services manually." -ForegroundColor Red
    exit 1
}

Write-Host "=== Starting QuicData Infrastructure ===" -ForegroundColor Cyan
docker compose up -d

Write-Host ""
Write-Host "Services:" -ForegroundColor Green
Write-Host "  PostgreSQL  localhost:5432  (quicdata / quicdata)" -ForegroundColor White
Write-Host "  Redis       localhost:6379" -ForegroundColor White
Write-Host ""
Write-Host "Copy backend/.env.example to backend/.env then start API + worker." -ForegroundColor Yellow
