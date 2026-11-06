# Copies this project to the server, runs the tests there, and restarts the bot.
# Usage: powershell -ExecutionPolicy Bypass -File deploy\push.ps1 -Server azureuser@1.2.3.4 -IdentityFile C:\path\to\key.pem
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$IdentityFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$archive = Join-Path $env:TEMP "pokerbot-deploy.tgz"
$sshArgs = @()
if ($IdentityFile) { $sshArgs = @("-i", $IdentityFile) }

tar.exe -czf $archive --exclude=logs --exclude=resume --exclude=__pycache__ --exclude=.pytest_cache --exclude=.mypy_cache -C $root .
if ($LASTEXITCODE -ne 0) { throw "Could not package the project." }

scp @sshArgs $archive "${Server}:pokerbot-deploy.tgz"
if ($LASTEXITCODE -ne 0) { throw "Could not copy the project to the server." }

# OneDrive folders are read-only on Windows, and tar carries that mode to the server.
$install = "sudo rm -rf pokerbot-src && mkdir pokerbot-src && tar -xzf pokerbot-deploy.tgz --delay-directory-restore --warning=no-unknown-keyword -C pokerbot-src && chmod -R u+rwX,go+rX pokerbot-src && sudo bash pokerbot-src/deploy/setup_server.sh"
ssh @sshArgs $Server $install
if ($LASTEXITCODE -ne 0) { throw "Setup failed, so the running bot was left unchanged." }
