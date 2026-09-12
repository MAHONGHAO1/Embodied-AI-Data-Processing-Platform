param([int]$Port = 8501, [string]$Episodes = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Episodes) {
    $env:ROBODATA_HDF5_EPISODES = $Episodes
    Write-Host ('数据范围：demo ' + $Episodes + '（范围变化后需重新导入批次）')
}
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw '未找到项目环境。请安装 Python 3.12 和 uv，然后在项目目录运行 uv sync --frozen。'
    }
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw '项目环境安装失败，请检查网络与安装日志。' }
}
Write-Host ('打开本机地址：http://127.0.0.1:' + $Port)
& $projectPython -m streamlit run (Join-Path $PSScriptRoot 'app.py') --server.address 127.0.0.1 --server.port $Port --browser.gatherUsageStats false
exit $LASTEXITCODE
