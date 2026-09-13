<#
.SYNOPSIS
    Run the gateway and the ithqbot worker directly from .venv (no containers).

.DESCRIPTION
    Prerequisites:
      1. docker compose up -d postgres kafka kafka-init
      2. host Redis listening on 127.0.0.1:6379

    Reads the repository root .env and rewrites container hostnames to 127.0.0.1.

    NOTE: this file is intentionally ASCII-only. Windows PowerShell decodes .ps1
    files using the ANSI code page on many hosts, so non-ASCII characters here
    would be mis-decoded and break parsing.

.EXAMPLE
    .\scripts\dev-local.ps1 -Target render
    .\scripts\dev-local.ps1 -Target gateway
    .\scripts\dev-local.ps1 -Target worker
#>
param(
    [ValidateSet('render', 'gateway', 'worker')]
    [string]$Target = 'render'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$ithqbotExe = Join-Path $root '.venv\Scripts\ithqbot.exe'

if (-not (Test-Path $py)) { throw "venv not found: $py" }

# ---- 1. load repository root .env (simple parser, skip comments/blank lines) ----
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) { return }
        $index = $line.IndexOf('=')
        if ($index -lt 1) { return }
        $key = $line.Substring(0, $index).Trim()
        $value = $line.Substring($index + 1).Trim()
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
}

# ---- 2. override hostnames so host-run processes can reach the containers ----
$env:POSTGRES_HOST = '127.0.0.1'
$env:KAFKA_SERVERS = '127.0.0.1:29092'
$env:REDIS_HOST = '127.0.0.1'
$env:ITHQBOT_WORKSPACE = (Join-Path $root 'services\ithqbot\workspace')
# session_scope=account_chat requires a shared workspace root (enterprise strict mode)
$env:ITHQBOT_SHARED_WORKSPACE = (Join-Path $root 'services\ithqbot\workspace\shared')
# host-run processes reach MinIO through the published port
$env:MINIO_ENDPOINT = "127.0.0.1:$($env:MINIO_PORT)"
$env:MINIO_ACCESS_KEY = $env:MINIO_ROOT_USER
$env:MINIO_SECRET_KEY = $env:MINIO_ROOT_PASSWORD
$env:MINIO_SECURE = 'false'
# gateway reads APP_MINIO_* (env_prefix APP_)
$env:APP_MINIO_ENDPOINT = $env:MINIO_ENDPOINT
$env:APP_MINIO_ACCESS_KEY = $env:MINIO_ACCESS_KEY
$env:APP_MINIO_SECRET_KEY = $env:MINIO_SECRET_KEY
$env:APP_MINIO_BUCKET = $env:MINIO_BUCKET
$env:APP_MINIO_SECURE = 'false'

$env:APP_DATABASE_URL = "postgresql+psycopg://$($env:POSTGRES_USER):$($env:POSTGRES_PASSWORD)@127.0.0.1:5432/$($env:POSTGRES_DB)"
$env:APP_REDIS_URI = 'redis://127.0.0.1:6379/0'
$env:APP_KAFKA_BOOTSTRAP_SERVERS = '127.0.0.1:29092'
if (-not $env:APP_KAFKA_INBOUND_TOPIC) { $env:APP_KAFKA_INBOUND_TOPIC = 'icatmsg_inbound' }
if (-not $env:APP_KAFKA_OUTBOUND_TOPIC) { $env:APP_KAFKA_OUTBOUND_TOPIC = 'icatmsg_outbound' }
if (-not $env:APP_KAFKA_OUTBOUND_GROUP) { $env:APP_KAFKA_OUTBOUND_GROUP = 'thqbot-gateway' }
if (-not $env:KAFKA_INBOUND_TOPIC) { $env:KAFKA_INBOUND_TOPIC = 'icatmsg_inbound' }
if (-not $env:KAFKA_OUTBOUND_TOPIC) { $env:KAFKA_OUTBOUND_TOPIC = 'icatmsg_outbound' }

# Safety net: httpx (used by the OpenAI SDK) raises InvalidURL when NO_PROXY
# contains a bracketed IPv6 loopback such as "[::1]". Drop those entries.
foreach ($proxyVar in @('NO_PROXY', 'no_proxy')) {
    $current = [Environment]::GetEnvironmentVariable($proxyVar)
    if (-not $current) { continue }
    $kept = @()
    foreach ($item in ($current -split ',')) {
        $trimmed = $item.Trim()
        if (-not $trimmed) { continue }
        if ($trimmed -eq '[::1]' -or $trimmed -eq '::1') { continue }
        $kept += $trimmed
    }
    [Environment]::SetEnvironmentVariable($proxyVar, ($kept -join ','), 'Process')
}

switch ($Target) {
    'render' {
        # ithqbot treats --config as a *bootstrap* only: the effective config is
        # loaded by FileConfigStore(<dir of --config>).load("default") from
        # <dir>/config.json. So the rendered result must BE config.json.
        $renderScript = Join-Path $root 'services\ithqbot\render_config.py'
        $template = Join-Path $root 'services\ithqbot\config.template.json'
        $out = Join-Path $root 'services\ithqbot\config.json'
        $renderArgs = @($renderScript, $template, $out)
        & $py @renderArgs
        if ($LASTEXITCODE -ne 0) { throw "render failed (exit=$LASTEXITCODE)" }
    }
    'gateway' {
        Push-Location (Join-Path $root 'services\gateway')
        try {
            & $py -m alembic upgrade head
            & $py -m uvicorn app.main:app --host 0.0.0.0 --port 8090
        }
        finally { Pop-Location }
    }
    'worker' {
        $config = Join-Path $root 'services\ithqbot\config.json'
        if (-not (Test-Path $config)) { throw "missing $config - run -Target render first" }
        & $ithqbotExe runtime --config $config --health-port 9002 --with-channels --with-cron --without-heartbeat
    }
}
