# RoboData Workbench 首次安装向导
# 自动完成：检查/安装 uv -> 准备 Python 3.12 -> 同步依赖 -> 下载公开样本 -> 创建桌面快捷方式

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
try { $host.UI.RawUI.WindowTitle = 'RoboData Workbench - First Time Setup' } catch {}

function Write-Step($n, $total, $text) {
    Write-Host ''
    Write-Host "[$n/$total] $text" -ForegroundColor Cyan
}
function Write-Ok($text)   { Write-Host "        OK    $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "        注意  $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "        失败  $text" -ForegroundColor Red }
function Write-Note($text) { Write-Host "              $text" -ForegroundColor DarkGray }

Write-Host ''
Write-Host '  RoboData Workbench  ·  具身数据工程工作台' -ForegroundColor White
Write-Host '  首次安装向导' -ForegroundColor White
Write-Host '  ------------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host '  本向导会准备好运行环境，并在桌面创建启动快捷方式。' -ForegroundColor Gray
Write-Host '  需要联网。主环境约 400 MB；完整转换链路另需约 2.2 GB（可选）。' -ForegroundColor Gray
Write-Host '  ------------------------------------------------------------------' -ForegroundColor DarkGray

$total = 5

# ---------- 1) uv ----------
Write-Step 1 $total '检查 uv（Python 环境管理器）'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Warn2 '未检测到 uv，尝试自动安装...'
    try {
        $installer = Join-Path $env:TEMP 'uv-install.ps1'
        Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer -TimeoutSec 90
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer | Out-Null
        $binDir = Join-Path $env:USERPROFILE '.local\bin'
        if (Test-Path -LiteralPath $binDir) { $env:Path = "$binDir;$env:Path" }
        $uv = Get-Command uv -ErrorAction SilentlyContinue
    } catch {
        Write-Bad "自动安装失败：$($_.Exception.Message)"
    }
}
if (-not $uv) {
    Write-Bad 'uv 不可用，无法继续。请手动安装后重新运行本向导：'
    Write-Note 'https://docs.astral.sh/uv/getting-started/installation/'
    Write-Note '若本机使用代理，请确认代理可用或配置直连后重试。'
    Read-Host '按回车退出'
    exit 1
}
Write-Ok "uv 就绪：$(& uv --version)"

# ---------- 2) Python 3.12 ----------
Write-Step 2 $total '准备 Python 3.12'
try {
    & uv python install 3.12
    Write-Ok 'Python 3.12 就绪（由 uv 托管，不改动系统 Python）'
} catch {
    Write-Bad "Python 安装失败：$($_.Exception.Message)"
    Read-Host '按回车退出'
    exit 1
}

# ---------- 3) 主环境依赖 ----------
Write-Step 3 $total '安装主环境依赖（页面、质检、报表，约 400 MB）'
try {
    & uv sync --frozen
    Write-Ok '主环境依赖安装完成'
} catch {
    Write-Bad "依赖安装失败：$($_.Exception.Message)"
    Write-Note '若长时间卡在下载，请检查网络或代理设置后重试。'
    Read-Host '按回车退出'
    exit 1
}

# ---------- 4) 公开样本 ----------
Write-Step 4 $total '下载公开演示数据（约 46 MB）'
$hdf5 = Join-Path $root 'data\robomimic\test.hdf5'
if (Test-Path -LiteralPath $hdf5) {
    Write-Ok '样本已存在，跳过下载'
} else {
    try {
        & uv run robodata fetch-hdf5
        Write-Ok '公开样本下载完成'
    } catch {
        Write-Warn2 "样本下载未完成：$($_.Exception.Message)"
        Write-Note '工作台仍可启动；导入功能在样本就绪后可用。'
        Write-Note '可稍后手动执行：uv run robodata fetch-hdf5'
    }
}

# ---------- 5) 桌面快捷方式 ----------
Write-Step 5 $total '在桌面创建启动快捷方式'
try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $launcher = Join-Path $root '启动工作台.cmd'
    if (-not (Test-Path -LiteralPath $launcher)) { throw "未找到启动脚本：$launcher" }
    $link = Join-Path $desktop 'RoboData 工作台.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = 'RoboData Workbench - 具身数据工程工作台'
    $shortcut.Save()
    Write-Ok '已创建桌面快捷方式：RoboData 工作台'
} catch {
    Write-Warn2 "快捷方式创建失败：$($_.Exception.Message)"
    Write-Note '可直接双击项目目录下的「启动工作台.cmd」'
}

Write-Host ''
Write-Host '  ------------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host '  安装完成。' -ForegroundColor Green
Write-Host '  双击桌面上的「RoboData 工作台」即可启动，浏览器会自动打开。' -ForegroundColor White
Write-Host '  ------------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  可选：需要完整 HDF5 转换链路（约 2.2 GB，含 CPU 版 PyTorch）时执行：' -ForegroundColor Gray
Write-Host '        uv sync --project tools/conversion --frozen' -ForegroundColor Gray
Write-Host '        uv run robodata fetch-hdf5' -ForegroundColor Gray
Write-Host ''
Read-Host '按回车退出'
