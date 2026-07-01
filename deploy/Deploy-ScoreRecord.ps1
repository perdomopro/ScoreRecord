param(
    [string]$HostName = "homeserver",
    [string]$User = "adrian",
    [string]$RemoteDir = "/home/adrian/scorerecord",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\scorerecord_homeserver",
    [switch]$SkipBootstrap
)

$ErrorActionPreference = "Stop"

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path $KeyPath)) {
    throw "SSH key not found at $KeyPath. Create and authorize it before deploying."
}

$root = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $env:TEMP "scorerecord-deploy.tar.gz"

Push-Location $root
try {
    git archive --format=tar.gz -o $archive HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "git archive failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Invoke-NativeChecked ssh @("-i", $KeyPath, "$User@$HostName", "mkdir -p '$RemoteDir'")
Invoke-NativeChecked scp @("-i", $KeyPath, $archive, "$User@$HostName`:/tmp/scorerecord-deploy.tar.gz")
Invoke-NativeChecked ssh @(
    "-i", $KeyPath, "$User@$HostName",
    "cd '$RemoteDir' && tar -xzf /tmp/scorerecord-deploy.tar.gz && rm /tmp/scorerecord-deploy.tar.gz"
)

if (-not $SkipBootstrap) {
    Invoke-NativeChecked ssh @(
        "-i", $KeyPath, "$User@$HostName",
        "cd '$RemoteDir' && bash deploy/bootstrap_homeserver.sh"
    )
}

Invoke-NativeChecked ssh @(
    "-i", $KeyPath, "$User@$HostName",
    "cd '$RemoteDir' && .venv/bin/scorerecord migrate-db && .venv/bin/scorerecord db-health && .venv/bin/scorerecord model-manifest && .venv/bin/scorerecord fetch-odds --dry-run --min-interval-seconds 0 && .venv/bin/scorerecord db-summary"
)
