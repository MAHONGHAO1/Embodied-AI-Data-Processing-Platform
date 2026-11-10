param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw '请先安装 Python 3.12 和 uv。' }
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) { throw '请先安装 Node.js 24 LTS。' }
$nodeVersion = (& node --version).Trim()
if ($nodeVersion -notmatch '^v24\.') { throw "前端要求 Node.js 24 LTS；当前版本为 $nodeVersion。" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw '未找到 npm，请重新安装 Node.js 24 LTS。' }
if (-not (Test-Path -LiteralPath 'frontend\node_modules')) {
    Push-Location frontend
    try { npm ci; if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' } }
    finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
    Push-Location frontend
    try { npm run build; if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' } }
    finally { Pop-Location }
}
uv sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Python 锁定依赖安装失败。' }
$env:ROBODATA_WEB_PORT = [string]$Port
Write-Host "RoboData 本地工作台：http://127.0.0.1:$Port"
uv run --locked python -m robodata.web
exit $LASTEXITCODE
