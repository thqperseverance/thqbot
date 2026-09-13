<#
.SYNOPSIS
    Rename the project directory to `thqbot` and repair the paths that point at it.

.DESCRIPTION
    Windows refuses to rename a directory while any process has it as its current
    directory. In practice this means you must run this script with NOTHING inside
    the project folder:

      * close any editor / terminal whose working directory is the project,
      * stop the gateway and the worker (and this DSH session, if it was started
        from inside the folder),
      * then run this script from a PowerShell window started elsewhere.

    The script performs, in order:
      1. stop the docker compose stack (containers + project volumes),
      2. rename the directory,
      3. repair the venv editable install of the agent runtime,
      4. re-render services/ithqbot/config.json against the new absolute paths,
      5. print the commands to start everything again.

    This file is intentionally ASCII-only: Windows PowerShell decodes .ps1 files
    using the ANSI code page, so non-ASCII here would be mis-decoded and break
    parsing.

.EXAMPLE
    .\scripts\rename-project-dir.ps1
    .\scripts\rename-project-dir.ps1 -NewName thqbot -SkipCompose
#>
param(
    [string]$NewName = 'thqbot',
    [switch]$SkipCompose,
    [switch]$KeepVolumes
)

$ErrorActionPreference = 'Stop'

$currentRoot = Split-Path -Parent $PSScriptRoot
$parent = Split-Path -Parent $currentRoot
$targetRoot = Join-Path $parent $NewName

Write-Host "current project dir : $currentRoot"
Write-Host "target  project dir : $targetRoot"

if ((Split-Path -Leaf $currentRoot) -eq $NewName) {
    Write-Host "already named '$NewName' - nothing to do."
    exit 0
}
if (Test-Path $targetRoot) {
    throw "target already exists: $targetRoot"
}

# ---- 1. stop the stack ------------------------------------------------------
if (-not $SkipCompose) {
    $compose = Join-Path $currentRoot 'docker-compose.yml'
    if (Test-Path $compose) {
        Write-Host "`n[1/5] stopping docker compose stack ..."
        $downArgs = @('compose', '-f', $compose, 'down', '--remove-orphans')
        if (-not $KeepVolumes) { $downArgs += '-v' }
        & docker @downArgs
        if ($LASTEXITCODE -ne 0) { Write-Warning "docker compose down returned $LASTEXITCODE (continuing)" }
    }
}

# ---- 2. rename -------------------------------------------------------------
Write-Host "`n[2/5] renaming directory ..."
try {
    Rename-Item -Path $currentRoot -NewName $NewName -ErrorAction Stop
} catch {
    Write-Host ""
    Write-Host "FAILED: the directory is still in use." -ForegroundColor Red
    Write-Host "Something has it open (editor, terminal, docker bind mount, or the"
    Write-Host "DSH session itself if it was launched from inside this folder)."
    Write-Host "Close it and re-run this script."
    throw
}
Write-Host "renamed -> $targetRoot"

# ---- 3. repair the editable install ---------------------------------------
Write-Host "`n[3/5] repairing venv editable install ..."
$py = Join-Path $targetRoot '.venv\Scripts\python.exe'
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $uv)) { $uv = 'uv' }
if (Test-Path $py) {
    & $uv pip uninstall --python $py ithqbot-ai 2>$null | Out-Null
    & $uv pip install --python $py --no-deps -e (Join-Path $targetRoot 'services\ithqbot')
    if ($LASTEXITCODE -ne 0) { Write-Warning "editable install returned $LASTEXITCODE" }
    & $py -c "import ithqbot; print(' import ok ->', ithqbot.__file__)"
} else {
    Write-Warning "no venv found at $py - recreate it, then reinstall requirements"
}

# ---- 4. re-render the agent config ---------------------------------------
Write-Host "`n[4/5] re-rendering services/ithqbot/config.json ..."
$render = Join-Path $targetRoot 'scripts\dev-local.ps1'
if (Test-Path $render) {
    & $render -Target render
} else {
    Write-Warning "dev-local.ps1 not found; run -Target render manually"
}

# ---- 5. next steps -------------------------------------------------------
Write-Host "`n[5/5] done. Next:" -ForegroundColor Green
Write-Host "  cd $targetRoot"
Write-Host "  docker compose up -d postgres kafka kafka-init minio"
Write-Host "  .\scripts\dev-local.ps1 -Target gateway     # terminal 1"
Write-Host "  .\scripts\dev-local.ps1 -Target worker      # terminal 2"
Write-Host ""
Write-Host "Note: database / bucket / tenant names already use 'thqbot' (they are"
Write-Host "set in .env), so a fresh volume is created on first start."
