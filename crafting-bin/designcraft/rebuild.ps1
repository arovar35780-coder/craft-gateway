# Rebuilds DesignCraft from ..\designcraft and refreshes designcraft.exe and designcraft-cli.exe (the MCP bridge) in this folder.
# Stops the copy of the app running from C:\dc_target first (a running exe cannot be overwritten).
$ErrorActionPreference = 'Stop'
$repo = Join-Path $PSScriptRoot '..\..\designcraft'
$env:CARGO_TARGET_DIR = 'C:\dc_target'
Get-Process designcraft -ErrorAction SilentlyContinue | Where-Object { $_.Path -in @('C:\dc_target\release\designcraft.exe', (Join-Path $PSScriptRoot 'designcraft.exe')) } | Stop-Process -Force
Start-Sleep 2
Push-Location $repo
try {
    cargo build --release -p designcraft -p designcraft-cli
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed ($LASTEXITCODE)" }   # check the exit code, a piped build hides failures
    Copy-Item 'C:\dc_target\release\designcraft.exe' (Join-Path $PSScriptRoot 'designcraft.exe') -Force
    try {
        Copy-Item 'C:\dc_target\release\designcraft-cli.exe' (Join-Path $PSScriptRoot 'designcraft-cli.exe') -Force
    } catch {
        # The bridge is running while a Claude Code session uses the MCP server; it cannot be overwritten then.
        Write-Warning 'designcraft-cli.exe (the MCP bridge) is in use and was not updated; close Claude Code and run this script again to update it.'
    }
    $commit = (git log -1 --format='%h %s')
    "Built from commit: $commit`nBuilt at: $(Get-Date -Format s)" | Set-Content (Join-Path $PSScriptRoot 'BUILD_INFO.txt') -Encoding utf8
} finally { Pop-Location }
Write-Host 'Done. Start with start-designcraft.cmd'
