param(
    [string]$IncludeLiveLoops = "true",
    [string]$IncludeLeapsLiveLoop = "true",
    [string]$IncludeUsLiveLoop = "true",
    [string]$IncludeReports = "true",
    [string]$IncludeEodScheduler = "true"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$LogPath = Join-Path $Root "data/runtime/startup/leaps_start_live_stack.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null

function Write-StartupLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogPath -Value "[$timestamp] $Message" -Encoding UTF8
}

function Find-ProcessByCommand {
    param([string[]]$Needles)
    $processes = Get-CimInstance Win32_Process -Filter "name = 'powershell.exe'"
    foreach ($process in $processes) {
        $command = [string]$process.CommandLine
        if (-not $command) {
            continue
        }
        $matched = $true
        foreach ($needle in $Needles) {
            if ($command -notlike "*$needle*") {
                $matched = $false
                break
            }
        }
        if ($matched) {
            return $process
        }
    }
    return $null
}

function Start-StackProcess {
    param(
        [string]$Name,
        [string[]]$Needles,
        [string[]]$Arguments
    )
    $existing = Find-ProcessByCommand -Needles $Needles
    if ($existing) {
        Write-StartupLog "$Name already running pid=$($existing.ProcessId)"
        return
    }
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList $Arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Write-StartupLog "$Name started pid=$($process.Id)"
}

function Test-FlagEnabled {
    param([string]$Value)
    return $Value -notmatch '^(false|0|no)$'
}

$includeLiveLoopsEnabled = Test-FlagEnabled $IncludeLiveLoops
$includeLeapsLiveLoopEnabled = Test-FlagEnabled $IncludeLeapsLiveLoop
$includeUsLiveLoopEnabled = Test-FlagEnabled $IncludeUsLiveLoop
$includeReportsEnabled = Test-FlagEnabled $IncludeReports
$includeEodSchedulerEnabled = Test-FlagEnabled $IncludeEodScheduler

Write-StartupLog "startup begin include_live_loops=$includeLiveLoopsEnabled include_leaps_live=$includeLeapsLiveLoopEnabled include_us_live=$includeUsLiveLoopEnabled include_reports=$includeReportsEnabled include_eod_scheduler=$includeEodSchedulerEnabled"

if ($includeLiveLoopsEnabled -and $includeLeapsLiveLoopEnabled) {
    Start-StackProcess `
        -Name "LEaps live order loop" `
        -Needles @("leaps_live_order_loop.ps1", "leaps_workspace_smoke.json", "LEaps") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_live_order_loop.ps1",
            "-Config", "configs/runtime/leaps_workspace_smoke.json",
            "-SleeveId", "LEaps",
            "-IntervalSeconds", "60",
            "-MaxSubmitNotional", "7000000",
            "-OrderBatchOutput", "data/runtime/live-order-loop/LEaps_candidate_orders.json",
            "-Journal", "data/cycle-journal/leaps_workspace_smoke.jsonl",
            "-LogPath", "data/runtime/live-order-loop/LEaps.log",
            "-SubmitStatePath", "data/runtime/live-order-loop/LEaps_submit_state.json",
            "-SubmitOncePerDay", "true"
        )

}

if ($includeLiveLoopsEnabled -and $includeUsLiveLoopEnabled) {
    Start-StackProcess `
        -Name "US ETF rotation live order loop" `
        -Needles @("leaps_live_order_loop.ps1", "us_etf_rotation_sleeve.json", "us_etf_rotation") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_live_order_loop.ps1",
            "-Config", "configs/runtime/us_etf_rotation_sleeve.json",
            "-SleeveId", "us_etf_rotation",
            "-IntervalSeconds", "300",
            "-MaxSubmitNotional", "2500",
            "-OrderBatchOutput", "data/runtime/live-order-loop/us_etf_rotation_candidate_orders.json",
            "-Journal", "data/cycle-journal/us_etf_rotation.jsonl",
            "-LogPath", "data/runtime/live-order-loop/us_etf_rotation.log",
            "-SubmitStatePath", "data/runtime/live-order-loop/us_etf_rotation_submit_state.json",
            "-SubmitOncePerDay", "true"
        )
}

if ($includeReportsEnabled) {
    Start-StackProcess `
        -Name "LEaps portfolio report loop" `
        -Needles @("leaps_portfolio_report_loop.ps1", "leaps_workspace_smoke.json", "LEaps") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_portfolio_report_loop.ps1",
            "-IntervalSeconds", "3600",
            "-Config", "configs/runtime/leaps_workspace_smoke.json",
            "-SleeveId", "LEaps",
            "-AccountStore", "data/virtual-accounts/kis_domestic.json",
            "-Title", "LEaps Portfolio Report",
            "-LogPath", "data/runtime/portfolio-reports/LEaps_portfolio_report_loop.log"
        )

    Start-StackProcess `
        -Name "US ETF rotation portfolio report loop" `
        -Needles @("leaps_portfolio_report_loop.ps1", "us_etf_rotation_sleeve.json", "us_etf_rotation") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_portfolio_report_loop.ps1",
            "-IntervalSeconds", "3600",
            "-Config", "configs/runtime/us_etf_rotation_sleeve.json",
            "-SleeveId", "us_etf_rotation",
            "-AccountStore", "data/virtual-accounts/kis_overseas.json",
            "-Title", "US ETF Rotation Portfolio Report",
            "-LogPath", "data/runtime/portfolio-reports/us_etf_rotation_portfolio_report_loop.log"
        )
}

if ($includeEodSchedulerEnabled) {
    Start-StackProcess `
        -Name "EOD snapshot scheduler" `
        -Needles @("leaps_eod_snapshot_scheduler.ps1") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_eod_snapshot_scheduler.ps1"
        )
}

Write-StartupLog "startup end"
