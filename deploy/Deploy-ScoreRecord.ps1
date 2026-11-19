param(
    [string]$HostName = "100.104.72.97",
    [string]$User = "adrian",
    [string]$RemoteDir = "/home/adrian/scorerecord",
    # Optional dedicated key; without it ssh uses your default identity.
    [string]$KeyPath = "$env:USERPROFILE\.ssh\scorerecord_homeserver",
    [switch]$SkipBootstrap
)

# Deploys the committed HEAD:
#   1. archives HEAD with git archive;
#   2. applies it with deploy/apply_release.sh, which also deletes files the
#      previous release shipped and this one dropped, and records the SHA in VERSION;
#   3. runs deploy/bootstrap_homeserver.sh (venv, migrations, systemd units);
#   4. restarts the dashboard and runs smoke checks.

$ErrorActionPreference = "Stop"

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

$identity = @()
if (Test-Path $KeyPath) { $identity = @("-i", $KeyPath) }
$target = "$User@$HostName"

$root = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $env:TEMP "scorerecord-deploy.tar.gz"

Push-Location $root
try {
    if (git status --porcelain) { Write-Warning "Uncommitted changes are NOT deployed; only HEAD is." }
    git archive --format=tar.gz -o $archive HEAD
    if ($LASTEXITCODE -ne 0) { throw "git archive failed with exit code $LASTEXITCODE" }
    $gitSha = (git rev-parse HEAD).Trim()
}
finally {
    Pop-Location
}

$remoteArchive = "/tmp/scorerecord-deploy.tar.gz"
Invoke-NativeChecked ssh ($identity + @($target, "mkdir -p '$RemoteDir'"))
Invoke-NativeChecked scp ($identity + @($archive, "${target}:$remoteArchive"))
Invoke-NativeChecked ssh ($identity + @(
    $target,
    "set -e; tar -xzf $remoteArchive -O deploy/apply_release.sh > /tmp/scorerecord-apply.sh; " +
    "bash /tmp/scorerecord-apply.sh $remoteArchive '$RemoteDir'; " +
    "echo '$gitSha' > '$RemoteDir/VERSION'; rm -f $remoteArchive /tmp/scorerecord-apply.sh"
))

if (-not $SkipBootstrap) {
    Invoke-NativeChecked ssh ($identity + @($target, "cd '$RemoteDir' && bash deploy/bootstrap_homeserver.sh"))
}

Invoke-NativeChecked ssh ($identity + @(
    $target,
    "cd '$RemoteDir' && systemctl --user restart scorerecord-gui.service && sleep 3 && " +
    "systemctl --user is-active scorerecord-gui.service && " +
    ".venv/bin/scorerecord db-health && .venv/bin/scorerecord model-manifest > /dev/null"
))
Write-Host "Deployed $gitSha to ${target}:$RemoteDir"
