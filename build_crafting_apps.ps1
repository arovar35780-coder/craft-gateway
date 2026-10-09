# Builds the Crafting Apps (release) one after another and copies the binaries to .\crafting-bin\<app>\.
# One shared target dir keeps common dependencies (egui, wgpu, ...) from being built six times.
# A failing app does not stop the others; see crafting-bin\logs\<app>.log and build-summary.log.
# Usage: build_crafting_apps.ps1 [app ...]      (default: all six, lightest first)
# With `powershell -File` extra words are not bound to an array parameter, so collect the remaining arguments
# and accept both "a b c" and "a,b,c".
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Apps)
if (-not $Apps) { $Apps = @('vectorcraft', 'lightcraft', 'photocraft', 'pdfcraft', 'filmcraft', 'effectcraft') }
# Upstream renamed PrintCraft to PdfCraft; a checkout made before that may still live in .\printcraft.
$oldFolders = @{ pdfcraft = 'printcraft' }
$Apps = @($Apps | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$root = $PSScriptRoot
$out = Join-Path $root 'crafting-bin'
$logs = Join-Path $out 'logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$env:CARGO_TARGET_DIR = 'C:\crafting_target'
$summary = Join-Path $out 'build-summary.log'
function Log($m) { $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m; $line | Add-Content $summary; Write-Host $line }
Log "start: $($Apps -join ', ')"
foreach ($app in $Apps) {
    $repo = Join-Path $root $app
    if (-not (Test-Path $repo) -and $oldFolders.ContainsKey($app)) { $repo = Join-Path $root $oldFolders[$app] }
    if (-not (Test-Path $repo)) { Log "${app}: SKIP (no folder)"; continue }
    $t0 = Get-Date
    Push-Location $repo
    try {
        # cmd /c keeps cargo's progress on stderr from being treated as errors by PowerShell
        cmd /c "cargo build --release --locked -p $app -p $app-cli > `"$logs\$app.log`" 2>&1"
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    if ($code -ne 0) { Log "${app}: FAILED exit=$code after $mins min (see logs\$app.log)"; continue }
    $dest = Join-Path $out $app
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    foreach ($exe in "$app.exe", "$app-cli.exe") {
        $src = Join-Path 'C:\crafting_target\release' $exe
        if (Test-Path $src) { Copy-Item $src $dest -Force } else { Log "${app}: WARNING $exe was not produced" }
    }
    $commit = git -C $repo log -1 --format='%h %s'
    "Built from commit: $commit`nBuilt at: $(Get-Date -Format s)" | Set-Content (Join-Path $dest 'BUILD_INFO.txt') -Encoding utf8
    Log "${app}: OK in $mins min -> $dest"
}
$free = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
Log "done. C: free $free GB"
