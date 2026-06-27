param(
    [string]$HostName = "homeserver",
    [string]$User = "adrian",
    [string]$RemoteDir = "/home/adrian/scorerecord",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\scorerecord_homeserver"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $KeyPath)) {
    throw "SSH key not found at $KeyPath"
}

$secret = Read-Host -Prompt "ODDS_API_IO_KEY"
if ([string]::IsNullOrWhiteSpace($secret)) {
    throw "ODDS_API_IO_KEY cannot be empty"
}

$remoteCommand = @"
cd '$RemoteDir' && \
IFS= read -r secret && \
tmp=`$(mktemp) && \
{ grep -v '^ODDS_API_IO_KEY=' .env > "`$tmp" || true; } && \
printf 'ODDS_API_IO_KEY=%s\n' "`$secret" >> "`$tmp" && \
mv "`$tmp" .env && \
chmod 600 .env
"@

$secret | ssh -i $KeyPath "$User@$HostName" $remoteCommand
if ($LASTEXITCODE -ne 0) {
    throw "Remote .env update failed with exit code $LASTEXITCODE"
}

Write-Host "Remote ODDS_API_IO_KEY updated."
