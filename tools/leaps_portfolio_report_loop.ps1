param(
    [int]$IntervalSeconds = 3600,
    [string]$Config = "configs/runtime/leaps_workspace_smoke.json",
    [string]$SleeveId = "LEaps",
    [string]$LogPath = "data/runtime/portfolio-reports/leaps_portfolio_report_loop.log",
    [string]$AccountStore = "data/virtual-accounts/kis_domestic.json",
    [string]$Title = "LEaps Portfolio Report"
)

$ErrorActionPreference = "Continue"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$logDir = Split-Path $LogPath -Parent
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

while ($true) {
    $started = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        "[$started] report start" | Out-File -FilePath $LogPath -Append -Encoding utf8
        py -3 tools/leaps_portfolio_report.py --config $Config --sleeve-id $SleeveId --account-store $AccountStore --title $Title --notify 2>&1 |
            Out-File -FilePath $LogPath -Append -Encoding utf8
        $completed = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "[$completed] report complete" | Out-File -FilePath $LogPath -Append -Encoding utf8
    }
    catch {
        $failed = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "[$failed] report failed: $($_.Exception.Message)" | Out-File -FilePath $LogPath -Append -Encoding utf8
    }
    Start-Sleep -Seconds $IntervalSeconds
}
