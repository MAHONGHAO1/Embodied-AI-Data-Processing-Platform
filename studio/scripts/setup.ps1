# QuicData setup for Windows
# Usage: powershell -ExecutionPolicy Bypass -File scripts/setup.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "=== QuicData Setup ===" -ForegroundColor Cyan

Write-Host "[1/4] Fetching QRDF SDK..." -ForegroundColor Green
& (Join-Path $Root "scripts\fetch-qrdf.ps1")

$Python = $null
foreach ($cmd in @("python", "python3", "py")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        $Python = $cmd
        break
    }
}
if (-not $Python) {
    Write-Host "ERROR: Python 3.10+ not found" -ForegroundColor Red
    exit 1
}

$version = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "Python: $Python ($version)"

$VenvDir = Join-Path $Root "backend\.venv"

if (-not (Test-Path $VenvDir)) {
    Write-Host "[2/4] Creating virtual environment..." -ForegroundColor Green
    & $Python -m venv $VenvDir
} else {
    Write-Host "[2/4] Virtual environment already exists" -ForegroundColor Yellow
}

$Activate = Join-Path $VenvDir "Scripts\Activate.ps1"
. $Activate

Write-Host "[3/4] Installing dependencies..." -ForegroundColor Green
$PypiIndexUrl = if ($env:PYPI_INDEX_URL) { $env:PYPI_INDEX_URL } else { "https://pypi.tuna.tsinghua.edu.cn/simple" }
Write-Host "PyPI mirror: $PypiIndexUrl" -ForegroundColor DarkGray
python -m pip install --index-url $PypiIndexUrl --upgrade pip -q
python -m pip install --index-url $PypiIndexUrl (Join-Path $Root "backend") -q

Write-Host "[4/4] Creating data directories and verifying QRDF..." -ForegroundColor Green
New-Item -ItemType Directory -Force -Path (Join-Path $Root "backend\data\storage") | Out-Null

$VendorDir = Join-Path $Root "backend\vendor\qrdf"
& python (Join-Path $Root "backend\scripts\verify_qrdf_sdk.py") --vendor-dir $VendorDir
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: dependencies installed but QRDF SDK lacks QuicData v0.2 APIs. Check vendor/qrdf and QRDF_GIT_REF." -ForegroundColor Red
    exit 1
}

$EnvExample = Join-Path $Root "backend\.env.example"
$EnvFile = Join-Path $Root "backend\.env"
if ((Test-Path $EnvExample) -and -not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Cyan
Write-Host "  QRDF:   backend/vendor/qrdf (Git fetch, decoupled from quicdata repo)" -ForegroundColor White
Write-Host "  Update: scripts/fetch-qrdf.ps1" -ForegroundColor White
Write-Host "  Local:  `$env:QRDF_LOCAL_PATH='..\qrdf'; scripts/fetch-qrdf.ps1" -ForegroundColor White
Write-Host "  API:    scripts/start-dev.ps1" -ForegroundColor White
Write-Host "URL: http://127.0.0.1:8000" -ForegroundColor White
Write-Host "Login: admin@quicdata.com / admin123" -ForegroundColor Yellow
