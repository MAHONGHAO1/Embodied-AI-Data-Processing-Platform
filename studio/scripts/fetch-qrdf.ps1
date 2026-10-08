# Fetch QRDF SDK into backend/vendor/qrdf (Windows)
# Default: track the QRDF develop branch.
# If a complete v0.2-compatible SDK already exists under backend/vendor/qrdf, skip remote fetch
# unless QRDF_FORCE_FETCH=1 or QRDF_LOCAL_PATH is set.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/fetch-qrdf.ps1
#   $env:QRDF_FORCE_FETCH = "1"; scripts/fetch-qrdf.ps1
#   $env:QRDF_GIT_REF = "main"; scripts/fetch-qrdf.ps1
#   $env:QRDF_GIT_DEPTH = "0"; scripts/fetch-qrdf.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VendorDir = Join-Path $Root "backend\vendor\qrdf"
$Config = if ($env:QRDF_ENV_FILE) { $env:QRDF_ENV_FILE } else { Join-Path $Root "deploy\qrdf.env" }
$DefaultGitUrl = "git@codeup.aliyun.com:6a3ce6c6a6fcee143fa25a90/QuicData/qrdf.git"

if (Test-Path $Config) {
    Get-Content $Config | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)\s*$') {
            Set-Item -Path "env:$($Matches[1])" -Value $Matches[2].Trim()
        }
    }
}

$GitUrl = if ($env:QRDF_GIT_URL) { $env:QRDF_GIT_URL } else { $DefaultGitUrl }
$GitRef = if ($env:QRDF_GIT_REF) { $env:QRDF_GIT_REF } else { "develop" }
$GitDepth = if ($env:QRDF_GIT_DEPTH) { $env:QRDF_GIT_DEPTH } else { "1" }
$ForceFetch = if ($env:QRDF_FORCE_FETCH) { $env:QRDF_FORCE_FETCH } else { "0" }

function Test-VendorSdkComplete {
    $pkgInit = Join-Path $VendorDir "qrdf\__init__.py"
    $pyproject = Join-Path $VendorDir "pyproject.toml"
    if (-not (Test-Path $pkgInit)) { return $false }
    if (-not (Test-Path $pyproject)) { return $false }
    $initText = Get-Content -Raw $pkgInit -ErrorAction SilentlyContinue
    if ($initText -match '0\.0\.0-stub|Minimal QRDF stub') { return $false }
    return $true
}

function Test-VendorSdkSupported {
    # Keep this source-level check dependency-free: fetch runs before pip install.
    $reader = Join-Path $VendorDir "qrdf\mcap\reader.py"
    $episode = Join-Path $VendorDir "qrdf\models\episode.py"
    if (-not (Test-Path $reader) -or -not (Test-Path $episode)) { return $false }
    $readerText = Get-Content -Raw $reader -ErrorAction SilentlyContinue
    $episodeText = Get-Content -Raw $episode -ErrorAction SilentlyContinue
    $hasSchemaProvenance = $readerText -match 'canonical_schema_name'
    $hasDescriptorFormat = $readerText -match 'descriptor_format'
    $hasLegacySchemaMarker = $readerText -match 'is_legacy_schema'
    $hasCanonicalSchemaLookup = $readerText -match 'def get_canonical_schema_name'
    $hasDataFileResolver = $episodeText -match 'def resolve_data_file'
    return $hasSchemaProvenance -and $hasDescriptorFormat -and $hasLegacySchemaMarker -and $hasCanonicalSchemaLookup -and $hasDataFileResolver
}

function Get-CurrentGitRef {
    if (-not (Test-Path (Join-Path $VendorDir ".git"))) { return "" }
    $branch = git -C $VendorDir symbolic-ref -q --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0) { return $branch }
    $head = git -C $VendorDir rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0) { return $head }
    return ""
}

function Fetch-RefFromOrigin {
    param([string]$Ref)
    if ($GitDepth -ne "0") {
        git -C $VendorDir fetch --depth $GitDepth origin $Ref 2>$null
    }
    git -C $VendorDir fetch --tags origin $Ref 2>$null
    if ($LASTEXITCODE -ne 0) {
        git -C $VendorDir fetch origin $Ref 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        git -C $VendorDir fetch --unshallow origin 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        git -C $VendorDir fetch origin
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed for ref: $Ref" }
    }
}

function Checkout-Ref {
    param([string]$Ref)
    git -C $VendorDir show-ref --verify --quiet "refs/remotes/origin/$Ref" 2>$null
    if ($LASTEXITCODE -eq 0) {
        git -C $VendorDir checkout -f -B $Ref "origin/$Ref"
        if ($LASTEXITCODE -ne 0) { throw "git checkout failed: origin/$Ref" }
        git -C $VendorDir reset --hard "origin/$Ref"
        if ($LASTEXITCODE -ne 0) { throw "git reset failed: origin/$Ref" }
        return
    }
    git -C $VendorDir rev-parse --verify FETCH_HEAD 2>$null
    if ($LASTEXITCODE -eq 0) {
        git -C $VendorDir checkout -f -B $Ref FETCH_HEAD
        if ($LASTEXITCODE -ne 0) { throw "git checkout failed: FETCH_HEAD -> $Ref" }
        git -C $VendorDir reset --hard FETCH_HEAD
        return
    }
    git -C $VendorDir show-ref --verify --quiet "refs/heads/$Ref" 2>$null
    if ($LASTEXITCODE -eq 0) {
        git -C $VendorDir checkout -f $Ref
        git -C $VendorDir pull --ff-only origin $Ref 2>$null
        if ($LASTEXITCODE -ne 0) { git -C $VendorDir reset --hard $Ref }
        return
    }
    throw "Unable to checkout QRDF ref: $Ref"
}

function Update-FromGit {
    Write-Host "Updating QRDF at $VendorDir ($GitRef)" -ForegroundColor Green
    Fetch-RefFromOrigin -Ref $GitRef
    Checkout-Ref -Ref $GitRef
}

function Clone-FromGit {
    Write-Host "Cloning $GitUrl -> $VendorDir (ref: $GitRef)" -ForegroundColor Green
    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("qrdf-vendor-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

    $cloned = $false
    if ($GitDepth -ne "0") {
        git clone --depth $GitDepth --single-branch --branch $GitRef $GitUrl $tmpDir 2>$null
        if ($LASTEXITCODE -eq 0) { $cloned = $true }
        if (-not $cloned) {
            git clone --depth $GitDepth $GitUrl $tmpDir 2>$null
            if ($LASTEXITCODE -eq 0) { $cloned = $true }
        }
    } else {
        git clone --single-branch --branch $GitRef $GitUrl $tmpDir 2>$null
        if ($LASTEXITCODE -eq 0) { $cloned = $true }
        if (-not $cloned) {
            git clone $GitUrl $tmpDir 2>$null
            if ($LASTEXITCODE -eq 0) { $cloned = $true }
        }
    }
    if (-not $cloned) {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
        Write-Host "ERROR: failed to clone QRDF repo: $GitUrl" -ForegroundColor Red
        Write-Host "  Check network/SSH and that remote branch '$GitRef' exists." -ForegroundColor Red
        exit 1
    }

    $currentRef = git -C $tmpDir symbolic-ref -q --short HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { $currentRef = "" }
    if ($currentRef -ne $GitRef) {
        if ($GitDepth -ne "0") {
            git -C $tmpDir fetch --depth $GitDepth origin $GitRef 2>$null
        }
        git -C $tmpDir fetch origin $GitRef 2>$null
        git -C $tmpDir show-ref --verify --quiet "refs/remotes/origin/$GitRef" 2>$null
        if ($LASTEXITCODE -eq 0) {
            git -C $tmpDir checkout -f -B $GitRef "origin/$GitRef"
        } else {
            git -C $tmpDir checkout -f -B $GitRef FETCH_HEAD
        }
    }

    if (Test-Path $VendorDir) { Remove-Item -Recurse -Force $VendorDir }
    New-Item -ItemType Directory -Force -Path (Split-Path $VendorDir) | Out-Null
    Move-Item -Path $tmpDir -Destination $VendorDir
}

Write-Host "=== Prepare QRDF SDK (ref: $GitRef) ===" -ForegroundColor Cyan

if ($env:QRDF_LOCAL_PATH) {
    $Src = Resolve-Path $env:QRDF_LOCAL_PATH
    if (-not (Test-Path (Join-Path $Src "qrdf"))) {
        Write-Host "ERROR: invalid local QRDF path: $Src" -ForegroundColor Red
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $VendorDir | Out-Null
    robocopy $Src $VendorDir /MIR /XD .git .venv __pycache__ /NFL /NDL /NJH /NJS | Out-Null
    Write-Host "Synced from local: $Src" -ForegroundColor Green
} elseif (($ForceFetch -ne "1") -and (Test-VendorSdkComplete) -and (Test-VendorSdkSupported)) {
    Write-Host "Compatible QRDF SDK already present, skip remote fetch: $VendorDir" -ForegroundColor Yellow
    Write-Host "  Force update: `$env:QRDF_FORCE_FETCH='1'; scripts/fetch-qrdf.ps1" -ForegroundColor DarkGray
} else {
    if ((Test-VendorSdkComplete) -and -not (Test-VendorSdkSupported)) {
        Write-Host "Existing QRDF SDK lacks the required v0.2 API; updating: $VendorDir" -ForegroundColor Yellow
    }
    if ($ForceFetch -eq "1") {
        Write-Host "QRDF_FORCE_FETCH=1, forcing remote fetch: $GitUrl" -ForegroundColor Yellow
    }
    if (Test-Path (Join-Path $VendorDir ".git")) {
        $currentRef = Get-CurrentGitRef
        if ($currentRef -and $currentRef -ne $GitRef) {
            Write-Host "Branch switch detected ($currentRef -> $GitRef), re-cloning..." -ForegroundColor Yellow
            Clone-FromGit
        } else {
            Update-FromGit
        }
    } else {
        Clone-FromGit
    }
}

$PkgInit = Join-Path $VendorDir "qrdf\__init__.py"
$Pyproject = Join-Path $VendorDir "pyproject.toml"
if (-not (Test-Path $PkgInit)) {
    Write-Host "ERROR: expected $PkgInit after fetch" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $Pyproject)) {
    Write-Host "ERROR: expected $Pyproject after fetch" -ForegroundColor Red
    exit 1
}
if (-not (Test-VendorSdkSupported)) {
    Write-Host "ERROR: QRDF SDK lacks QuicData v0.2 APIs (schema provenance, get_canonical_schema_name, resolve_data_file)." -ForegroundColor Red
    Write-Host "Use a compatible QRDF_GIT_REF or set QRDF_FORCE_FETCH=1 and retry." -ForegroundColor Red
    exit 1
}

$qrdfVersion = "unknown"
$versionFile = Join-Path $VendorDir "qrdf\version.py"
if (Test-Path $versionFile) {
    $m = Select-String -Path $versionFile -Pattern '^QRDF_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $qrdfVersion = $m.Matches.Groups[1].Value }
}

if (Test-Path (Join-Path $VendorDir ".git")) {
    $branch = git -C $VendorDir symbolic-ref -q --short HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { $branch = "detached" }
    $head = git -C $VendorDir rev-parse --short HEAD 2>$null
    Write-Host "QRDF Git: branch=$branch commit=$head version=$qrdfVersion" -ForegroundColor DarkGray
} else {
    Write-Host "QRDF Vendor: version=$qrdfVersion" -ForegroundColor DarkGray
}

# Soft import check only — full hard check happens after pip install in setup.ps1
$pyForCheck = $null
$venvPython = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pyForCheck = $venvPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pyForCheck = "python"
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $pyForCheck = "python3"
}

if ($pyForCheck) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $importOut = & $pyForCheck -c "import sys; sys.path.insert(0, r'$VendorDir'); import qrdf; print('QRDF', qrdf.__version__)" 2>&1
    $importCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($importCode -eq 0) {
        Write-Host ($importOut | Out-String).Trim()
    } elseif ("$importOut" -match 'ModuleNotFoundError|ImportError') {
        Write-Host "Hint: QRDF package layout is ready; runtime deps (e.g. numpy) will be verified after setup installs requirements." -ForegroundColor DarkGray
    } else {
        Write-Host "Warning: QRDF import check failed (non-blocking for fetch):" -ForegroundColor Yellow
        Write-Host ($importOut | Out-String).Trim() -ForegroundColor Yellow
    }
}

Write-Host "QRDF SDK ready: $VendorDir" -ForegroundColor Cyan
