# Registers the daily pipeline runs in Windows Task Scheduler (current user).
# Run once from the project root:  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
# Remove again:                    powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Remove
# Times are local; defaults assume CET/CEST (crypto after the 00:00 UTC daily close, stocks after the US close).

param(
    [string]$CryptoTime = "02:30",
    [string]$StocksTime = "23:45",
    [string]$UniverseDay = "SUN",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
$uv = (Get-Command uv).Source
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

$tasks = @(
    @{ Name = "SRC Crypto Daily";   Schedule = "/SC DAILY /ST $CryptoTime"; Args = "run --universe observed --class crypto" },
    @{ Name = "SRC Stocks Daily";   Schedule = "/SC WEEKLY /D MON,TUE,WED,THU,FRI /ST $StocksTime"; Args = "run --universe observed --class stock" },
    @{ Name = "SRC Universe Weekly"; Schedule = "/SC WEEKLY /D $UniverseDay /ST 12:00"; Args = "universe" }
)

foreach ($t in $tasks) {
    if ($Remove) {
        schtasks /Delete /TN $t.Name /F | Out-Null
        Write-Host "Removed $($t.Name)"
        continue
    }
    $log = Join-Path $logDir (($t.Name -replace ' ', '_') + ".log")
    $cmd = "cmd /c cd /d `"$root`" && `"$uv`" run src $($t.Args) >> `"$log`" 2>&1"
    $expr = "schtasks /Create /F /TN `"$($t.Name)`" $($t.Schedule) /TR '$cmd'"
    Invoke-Expression $expr | Out-Null
    Write-Host "Registered $($t.Name): $($t.Schedule) -> src $($t.Args)"
}
