param(
    [string]$HostName = "homeserver",
    [string]$User = "adrian",
    [string]$RemoteDir = "/home/adrian/scorerecord",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\scorerecord_homeserver",
    [switch]$SkipBootstrap
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $KeyPath)) {
    throw "SSH key not found at $KeyPath. Create and authorize it before deploying."
}

$root = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $env:TEMP "scorerecord-deploy.tar.gz"

Push-Location $root
try {
    git archive --format=tar.gz -o $archive HEAD
}
finally {
    Pop-Location
}

ssh -i $KeyPath "$User@$HostName" "mkdir -p '$RemoteDir'"
scp -i $KeyPath $archive "$User@$HostName`:/tmp/scorerecord-deploy.tar.gz"
ssh -i $KeyPath "$User@$HostName" "cd '$RemoteDir' && tar -xzf /tmp/scorerecord-deploy.tar.gz && rm /tmp/scorerecord-deploy.tar.gz"

if (-not $SkipBootstrap) {
    ssh -i $KeyPath "$User@$HostName" "cd '$RemoteDir' && bash deploy/bootstrap_homeserver.sh"
}

ssh -i $KeyPath "$User@$HostName" "cd '$RemoteDir' && .venv/bin/scorerecord model-manifest && .venv/bin/scorerecord fetch-odds --dry-run --min-interval-seconds 0 && .venv/bin/scorerecord db-summary"
