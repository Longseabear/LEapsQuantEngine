param(
    [string]$IncludeLiveLoops = "true",
    [string]$IncludeMultiSleeveLiveLoop = "true",
    [string]$IncludeLeapsLiveLoop = "false",
    [string]$IncludeUsLiveLoop = "false",
    [string]$IncludeReports = "true",
    [string]$IncludeEodScheduler = "true",
    [string]$SeedRuntimeState = "true"
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
$includeMultiSleeveLiveLoopEnabled = Test-FlagEnabled $IncludeMultiSleeveLiveLoop
$includeLeapsLiveLoopEnabled = Test-FlagEnabled $IncludeLeapsLiveLoop
$includeUsLiveLoopEnabled = Test-FlagEnabled $IncludeUsLiveLoop
$includeReportsEnabled = Test-FlagEnabled $IncludeReports
$includeEodSchedulerEnabled = Test-FlagEnabled $IncludeEodScheduler
$seedRuntimeStateEnabled = Test-FlagEnabled $SeedRuntimeState

Write-StartupLog "startup begin include_live_loops=$includeLiveLoopsEnabled include_multi_live=$includeMultiSleeveLiveLoopEnabled include_leaps_live=$includeLeapsLiveLoopEnabled include_us_live=$includeUsLiveLoopEnabled include_reports=$includeReportsEnabled include_eod_scheduler=$includeEodSchedulerEnabled seed_runtime_state=$seedRuntimeStateEnabled"

if ($seedRuntimeStateEnabled) {
    $env:PYTHONPATH = "src"
    $seedArgs = @(
        "-3", "-m", "leaps_quant_engine.cli", "runtime-state-seed-trailing-stop",
        "configs/runtime/live_multi_sleeve.json",
        "--sleeve-id", "LEaps",
        "--account-store", (Join-Path $Root "data/virtual-accounts/kis_domestic.json"),
        "--runtime-state", (Join-Path $Root "data/runtime/runtime-state/live_multi_sleeve.sqlite"),
        "--summary-only"
    )
    $seedOutput = py @seedArgs 2>&1
    $seedExit = $LASTEXITCODE
    $seedOutput | Add-Content -Path $LogPath -Encoding UTF8
    Write-StartupLog "runtime-state seed exit=$seedExit sleeve=LEaps"
}

if ($includeLiveLoopsEnabled -and $includeMultiSleeveLiveLoopEnabled) {
    Start-StackProcess `
        -Name "Multi-sleeve live order loop" `
        -Needles @("leaps_multi_sleeve_live_order_loop.ps1", "live_multi_sleeve.json", "LEaps", "us_etf_rotation") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_multi_sleeve_live_order_loop.ps1",
            "-Config", "configs/runtime/live_multi_sleeve.json",
            "-SleeveIds", "LEaps,us_etf_rotation",
            "-IntervalSeconds", "60",
            "-OrderBatchOutput", "data/runtime/live-order-loop/multi_sleeve_candidate_orders.json",
            "-Journal", "data/cycle-journal/live_multi_sleeve.jsonl",
            "-LogPath", "data/runtime/live-order-loop/multi_sleeve.log",
            "-FrameworkStateDir", "data/runtime/framework-state/multi-sleeve",
            "-RuntimeStatePath", "data/runtime/runtime-state/live_multi_sleeve.sqlite",
            "-SubmitStatePath", "data/runtime/live-order-loop/multi_sleeve_submit_state.json",
            "-ControlQueue", "data/runtime/control/live.jsonl",
            "-ActiveSleevesPath", "data/runtime/live-order-loop/multi_sleeve_active_sleeves.json",
            "-HotReload", "true",
            "-StaleAfterSeconds", "300",
            "-CancelStaleOpenTickets", "true",
            "-ExpireDayOpenTickets", "true"
        )
}

if ($includeLiveLoopsEnabled -and $includeLeapsLiveLoopEnabled) {
    Start-StackProcess `
        -Name "LEaps legacy single-sleeve live order loop" `
        -Needles @("leaps_live_order_loop.ps1", "live_multi_sleeve.json", "LEaps") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_live_order_loop.ps1",
            "-Config", "configs/runtime/live_multi_sleeve.json",
            "-SleeveId", "LEaps",
            "-IntervalSeconds", "60",
            "-OrderBatchOutput", "data/runtime/live-order-loop/LEaps_candidate_orders.json",
            "-Journal", "data/cycle-journal/live_multi_sleeve.jsonl",
            "-LogPath", "data/runtime/live-order-loop/LEaps.log",
            "-SubmitStatePath", "data/runtime/live-order-loop/LEaps_submit_state.json",
            "-RuntimeStatePath", "data/runtime/runtime-state/live_multi_sleeve.sqlite",
            "-SubmitOncePerDay", "true",
            "-StaleAfterSeconds", "300",
            "-CancelStaleOpenTickets", "true",
            "-ExpireDayOpenTickets", "true"
        )

}

if ($includeLiveLoopsEnabled -and $includeUsLiveLoopEnabled) {
    Start-StackProcess `
        -Name "US ETF rotation legacy single-sleeve live order loop" `
        -Needles @("leaps_live_order_loop.ps1", "live_multi_sleeve.json", "us_etf_rotation") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_live_order_loop.ps1",
            "-Config", "configs/runtime/live_multi_sleeve.json",
            "-SleeveId", "us_etf_rotation",
            "-IntervalSeconds", "300",
            "-OrderBatchOutput", "data/runtime/live-order-loop/us_etf_rotation_candidate_orders.json",
            "-Journal", "data/cycle-journal/us_etf_rotation.jsonl",
            "-LogPath", "data/runtime/live-order-loop/us_etf_rotation.log",
            "-SubmitStatePath", "data/runtime/live-order-loop/us_etf_rotation_submit_state.json",
            "-RuntimeStatePath", "data/runtime/runtime-state/live_multi_sleeve.sqlite",
            "-SubmitOncePerDay", "true",
            "-StaleAfterSeconds", "900",
            "-CancelStaleOpenTickets", "true",
            "-ExpireDayOpenTickets", "true"
        )
}

if ($includeReportsEnabled) {
    Start-StackProcess `
        -Name "LEaps portfolio report loop" `
        -Needles @("leaps_portfolio_report_loop.ps1", "live_multi_sleeve.json", "LEaps") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_portfolio_report_loop.ps1",
            "-IntervalSeconds", "60",
            "-Config", "configs/runtime/live_multi_sleeve.json",
            "-SleeveId", "LEaps",
            "-AccountStore", "data/virtual-accounts/kis_domestic.json",
            "-Title", "LEaps",
            "-ReportMode", "latest-target",
            "-LogPath", "data/runtime/portfolio-reports/LEaps_portfolio_report_loop.log",
            "-ScheduleMode", "phase",
            "-MarketScope", "domestic",
            "-StatePath", "data/runtime/portfolio-reports/LEaps_portfolio_report_loop.state.json"
        )

    Start-StackProcess `
        -Name "US ETF rotation portfolio report loop" `
        -Needles @("leaps_portfolio_report_loop.ps1", "live_multi_sleeve.json", "us_etf_rotation") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools/leaps_portfolio_report_loop.ps1",
            "-IntervalSeconds", "300",
            "-Config", "configs/runtime/live_multi_sleeve.json",
            "-SleeveId", "us_etf_rotation",
            "-AccountStore", "data/virtual-accounts/kis_overseas.json",
            "-Title", "US_ETF",
            "-ReportMode", "latest-target",
            "-LogPath", "data/runtime/portfolio-reports/us_etf_rotation_portfolio_report_loop.log",
            "-ScheduleMode", "phase",
            "-MarketScope", "overseas",
            "-StatePath", "data/runtime/portfolio-reports/us_etf_rotation_portfolio_report_loop.state.json"
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
