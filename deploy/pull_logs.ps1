# Downloads the server's session logs into logs\server.
# Usage: powershell -ExecutionPolicy Bypass -File deploy\pull_logs.ps1 -Server azureuser@1.2.3.4 -IdentityFile C:\path\to\key.pem
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$IdentityFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$target = Join-Path $root "logs\server"
$archive = Join-Path $env:TEMP "pokerbot-logs.tgz"
$sshArgs = @()
if ($IdentityFile) { $sshArgs = @("-i", $IdentityFile) }

$pack = "sudo tar -czf pokerbot-logs.tgz -C /home/pokerbot/pokerBot/logs . && sudo chown `$(id -u) pokerbot-logs.tgz"
ssh @sshArgs $Server $pack
if ($LASTEXITCODE -ne 0) { throw "Could not package the logs on the server." }

scp @sshArgs "${Server}:pokerbot-logs.tgz" $archive
if ($LASTEXITCODE -ne 0) { throw "Could not download the logs." }

New-Item -ItemType Directory -Force $target | Out-Null
tar.exe -xzf $archive -C $target
if ($LASTEXITCODE -ne 0) { throw "Could not unpack the logs." }
Write-Output "Logs are in $target"
