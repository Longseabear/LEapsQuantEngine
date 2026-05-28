param(
    [string]$Config = "configs/runtime/live_multi_sleeve.json",
    [string]$ActiveSleevesPath = "data/runtime/live-order-loop/multi_sleeve_active_sleeves.json",
    [string[]]$DefaultSleeveIds = @("kr-lowvol-defensive", "us_etf_rotation"),
    [string]$IncludeGateway = "true",
    [string]$IncludeBroker = "true",
    [string]$RunPreflight = "true",
    [string]$IncludeLiveLoop = "true",
    [string]$IncludeReports = "true",
    [string]$IncludeEodScheduler = "true",
    [string]$RestartUnhealthyServices = "true",
    [string]$DryRun = "false",
    [int]$VerifySeconds = 45,
    [string]$GatewayUrl = "http://127.0.0.1:8766",
    [string]$BrokerUrl = "http://127.0.0.1:8755",
    [string]$LiveLoopHeartbeatPath = "data/runtime/live-order-loop/multi_sleeve_heartbeat.json",
    [int]$LiveLoopHeartbeatMaxAgeSeconds = 120,
    [string]$UseProcessScan = "false",
    [string]$StatusPath = "data/runtime/startup/leaps_safe_start_live_stack_status.json",
    [string]$LogPath = "data/runtime/startup/leaps_safe_start_live_stack.log"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONPATH = "src"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Resolve-RepoPath {
    param([string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return (Join-Path $Root $PathValue)
}

$logFullPath = Resolve-RepoPath $LogPath
$statusFullPath = Resolve-RepoPath $StatusPath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFullPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $statusFullPath) | Out-Null

function Write-SafeStartLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFullPath -Value "[$timestamp] $Message" -Encoding UTF8
}

function Test-FlagEnabled {
    param([string]$Value)
    return $Value -notmatch '^(false|0|no)$'
}

function Normalize-SleeveIds {
    param([object[]]$Items)
    $seen = @{}
    $result = @()
    foreach ($item in $Items) {
        foreach ($part in ([string]$item -split ",")) {
            $value = $part.Trim()
            if ($value -and -not $seen.ContainsKey($value)) {
                $seen[$value] = $true
                $result += $value
            }
        }
    }
    return @($result)
}

function Get-ActiveSleeveIds {
    $activePath = Resolve-RepoPath $ActiveSleevesPath
    if (Test-Path $activePath) {
        try {
            $payload = Get-Content $activePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $active = @(Normalize-SleeveIds -Items @($payload.active_sleeve_ids))
            if ($active.Count -gt 0) {
                return $active
            }
            Write-SafeStartLog "active sleeve file had no active_sleeve_ids path=$activePath"
        } catch {
            Write-SafeStartLog "active sleeve file parse failed path=$activePath error=$($_.Exception.Message)"
        }
    } else {
        Write-SafeStartLog "active sleeve file missing path=$activePath using defaults"
    }
    return @(Normalize-SleeveIds -Items $DefaultSleeveIds)
}

function Find-ProcessByCommand {
    param([string[]]$Needles)
    if (-not (Test-FlagEnabled $UseProcessScan)) {
        return $null
    }
    $processes = Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match 'powershell|python|py' }
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

function Find-ProcessesByCommand {
    param([string[]]$Needles)
    if (-not (Test-FlagEnabled $UseProcessScan)) {
        return @()
    }
    $foundProcesses = @()
    $processes = Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match 'powershell|python|py' }
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
            $foundProcesses += $process
        }
    }
    return @($foundProcesses)
}

function Invoke-Health {
    param(
        [string]$Name,
        [string]$Url
    )
    try {
        $payload = Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 3
        $status = [string]$payload.status
        if ($status -eq "ok") {
            return [ordered]@{
                name = $Name
                ok = $true
                url = $Url
                payload = $payload
                error = $null
            }
        }
        return [ordered]@{
            name = $Name
            ok = $false
            url = $Url
            payload = $payload
            error = "health_status=$status"
        }
    } catch {
        return [ordered]@{
            name = $Name
            ok = $false
            url = $Url
            payload = $null
            error = $_.Exception.Message
        }
    }
}

function Stop-MatchedProcesses {
    param(
        [string]$Name,
        [string[]]$Needles
    )
    $stopped = @()
    foreach ($process in @(Find-ProcessesByCommand -Needles $Needles)) {
        if ($process.ProcessId -eq $PID) {
            continue
        }
        if (Test-FlagEnabled $DryRun) {
            Write-SafeStartLog "dry-run would stop $Name pid=$($process.ProcessId)"
            $stopped += $process.ProcessId
            continue
        }
        try {
            Stop-Process -Id $process.ProcessId -Force
            Write-SafeStartLog "$Name stale process stopped pid=$($process.ProcessId)"
            $stopped += $process.ProcessId
        } catch {
            Write-SafeStartLog "$Name stale process stop failed pid=$($process.ProcessId) error=$($_.Exception.Message)"
        }
    }
    return @($stopped)
}

function Start-StackProcess {
    param(
        [string]$Name,
        [string[]]$Needles,
        [string[]]$Arguments,
        [string]$StdoutPath = "",
        [string]$StderrPath = "",
        [switch]$SkipExistingProcessCheck
    )
    if (-not $SkipExistingProcessCheck) {
        $existing = Find-ProcessByCommand -Needles $Needles
        if ($existing) {
            Write-SafeStartLog "$Name already running pid=$($existing.ProcessId)"
            return [ordered]@{
                name = $Name
                action = "already_running"
                pid = $existing.ProcessId
            }
        }
    }
    if (Test-FlagEnabled $DryRun) {
        Write-SafeStartLog "dry-run would start $Name args=$($Arguments -join ' ')"
        return [ordered]@{
            name = $Name
            action = "dry_run_start"
            pid = $null
        }
    }
    $startArgs = @{
        FilePath = "powershell.exe"
        ArgumentList = $Arguments
        WorkingDirectory = $Root
        WindowStyle = "Hidden"
        PassThru = $true
    }
    if ($StdoutPath) {
        $stdoutFullPath = Resolve-RepoPath $StdoutPath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stdoutFullPath) | Out-Null
        Remove-Item -LiteralPath $stdoutFullPath -ErrorAction SilentlyContinue
        $startArgs.RedirectStandardOutput = $stdoutFullPath
    }
    if ($StderrPath) {
        $stderrFullPath = Resolve-RepoPath $StderrPath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stderrFullPath) | Out-Null
        Remove-Item -LiteralPath $stderrFullPath -ErrorAction SilentlyContinue
        $startArgs.RedirectStandardError = $stderrFullPath
    }
    $process = Start-Process @startArgs
    Write-SafeStartLog "$Name started pid=$($process.Id)"
    return [ordered]@{
        name = $Name
        action = "started"
        pid = $process.Id
    }
}

function Start-KisGatewayIfNeeded {
    $health = Invoke-Health -Name "kis_gateway" -Url $GatewayUrl
    if ($health.ok) {
        Write-SafeStartLog "KIS gateway health ok url=$GatewayUrl"
        $process = [ordered]@{
            name = "KIS gateway"
            action = "healthy_http"
            pid = $null
        }
        return [ordered]@{
            health = $health
            process = $process
        }
    }

    Write-SafeStartLog "KIS gateway health failed error=$($health.error)"
    if ((Test-FlagEnabled $RestartUnhealthyServices) -and (Test-FlagEnabled $UseProcessScan)) {
        Stop-MatchedProcesses -Name "KIS gateway" -Needles @("kis-gateway-serve") | Out-Null
        Start-Sleep -Seconds 1
    } elseif (Test-FlagEnabled $RestartUnhealthyServices) {
        Write-SafeStartLog "KIS gateway stale process scan skipped; HTTP health is the liveness source"
    }
    $process = Start-StackProcess `
        -Name "KIS gateway" `
        -Needles @("kis-gateway-serve", "8766") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", "`$env:PYTHONPATH='src'; py -3 -m leaps_quant_engine.cli kis-gateway-serve --host 127.0.0.1 --port 8766"
        ) `
        -StdoutPath "data/runtime/startup/kis_gateway_stdout.log" `
        -StderrPath "data/runtime/startup/kis_gateway_stderr.log" `
        -SkipExistingProcessCheck
    Start-Sleep -Seconds 5
    $after = Invoke-Health -Name "kis_gateway" -Url $GatewayUrl
    return [ordered]@{
        health = $after
        process = $process
    }
}

function Start-BrokerEngineIfNeeded {
    $health = Invoke-Health -Name "broker_engine" -Url $BrokerUrl
    if ($health.ok) {
        Write-SafeStartLog "broker-engine health ok url=$BrokerUrl"
        $process = [ordered]@{
            name = "broker-engine"
            action = "healthy_http"
            pid = $null
        }
        return [ordered]@{
            health = $health
            process = $process
        }
    }

    Write-SafeStartLog "broker-engine health failed error=$($health.error)"
    if ((Test-FlagEnabled $RestartUnhealthyServices) -and (Test-FlagEnabled $UseProcessScan)) {
        Stop-MatchedProcesses -Name "broker-engine" -Needles @("broker_engine.app") | Out-Null
        Stop-MatchedProcesses -Name "broker-engine wrapper" -Needles @("run_broker_engine.ps1") | Out-Null
        Start-Sleep -Seconds 1
    } elseif (Test-FlagEnabled $RestartUnhealthyServices) {
        Write-SafeStartLog "broker-engine stale process scan skipped; HTTP health is the liveness source"
    }
    $process = Start-StackProcess `
        -Name "broker-engine" `
        -Needles @("broker_engine.app", "8755") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "reference\stockprogram_legacy\scripts\windows\broker-engine\run_broker_engine.ps1"
        ) `
        -StdoutPath "data/runtime/startup/broker_engine_stdout.log" `
        -StderrPath "data/runtime/startup/broker_engine_stderr.log" `
        -SkipExistingProcessCheck
    Start-Sleep -Seconds 5
    $after = Invoke-Health -Name "broker_engine" -Url $BrokerUrl
    return [ordered]@{
        health = $after
        process = $process
    }
}

function Invoke-LivePreflight {
    param([string[]]$SleeveIds)
    $args = @(
        "-3", "-m", "leaps_quant_engine.cli", "runtime-preflight", $Config,
        "--include-order-status",
        "--strict-live"
    )
    foreach ($sleeveId in $SleeveIds) {
        $args += @("--sleeve-id", $sleeveId)
    }
    Write-SafeStartLog "preflight begin sleeves=$($SleeveIds -join ',')"
    $output = py @args 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String)
    $report = $null
    $blocked = @()
    try {
        $report = $text | ConvertFrom-Json
        foreach ($check in @($report.checks)) {
            $checkStatus = [string]$check.status
            if ($checkStatus -in @("critical", "error", "blocked", "failed")) {
                $blocked += "$($check.name):$checkStatus"
            }
            if ($check.name -in @("open_tickets", "unallocated_fills")) {
                $count = 0
                try {
                    $count = [int]$check.metadata.count
                } catch {
                    $count = 0
                }
                if ($count -gt 0) {
                    $blocked += "$($check.name):$($count):$($check.metadata.account_id)"
                }
            }
        }
    } catch {
        $blocked += "preflight_json_parse_failed"
        Write-SafeStartLog "preflight JSON parse failed error=$($_.Exception.Message)"
    }

    if ($exitCode -ne 0) {
        $blocked += "preflight_exit_code:$exitCode"
    }
    Write-SafeStartLog "preflight end exit=$exitCode status=$($report.status) blocked=$($blocked -join ',')"
    return [ordered]@{
        exit_code = $exitCode
        status = if ($report) { $report.status } else { "unparsed" }
        blocked_reasons = @($blocked)
        output_path = $null
        report = $report
    }
}

function Get-HeartbeatStatus {
    param(
        [string]$PathValue,
        [string]$Component,
        [int]$MaxAgeSeconds,
        [string[]]$AllowedStatuses = @("running", "idle", "paused")
    )
    $path = Resolve-RepoPath $PathValue
    if (-not (Test-Path $path)) {
        return [ordered]@{
            ok = $false
            path = $path
            reason = "heartbeat_missing"
            payload = $null
            age_seconds = $null
        }
    }
    try {
        $payload = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        $updatedAt = [datetime]::Parse([string]$payload.updated_at)
        $age = [Math]::Max(0, ((Get-Date) - $updatedAt).TotalSeconds)
        $status = [string]$payload.status
        $actualComponent = [string]$payload.component
        $ok = (
            $age -le $MaxAgeSeconds -and
            $actualComponent -eq $Component -and
            $status -in $AllowedStatuses
        )
        return [ordered]@{
            ok = $ok
            path = $path
            reason = if ($ok) { "" } elseif ($age -gt $MaxAgeSeconds) { "heartbeat_stale" } else { "heartbeat_status_or_component_invalid" }
            payload = $payload
            age_seconds = $age
        }
    } catch {
        return [ordered]@{
            ok = $false
            path = $path
            reason = "heartbeat_parse_failed:$($_.Exception.Message)"
            payload = $null
            age_seconds = $null
        }
    }
}

function Get-LiveLoopHeartbeat {
    return Get-HeartbeatStatus -PathValue $LiveLoopHeartbeatPath -Component "multi_sleeve_live_order_loop" -MaxAgeSeconds $LiveLoopHeartbeatMaxAgeSeconds
}

function Start-MultiSleeveLoop {
    param([string[]]$SleeveIds)
    $heartbeat = Get-LiveLoopHeartbeat
    if ($heartbeat.ok) {
        $pidValue = $null
        try {
            $pidValue = $heartbeat.payload.process_id
        } catch {
            $pidValue = $null
        }
        Write-SafeStartLog "multi-sleeve live order loop healthy by heartbeat age_seconds=$([Math]::Round([double]$heartbeat.age_seconds, 1)) path=$($heartbeat.path)"
        return [ordered]@{
            name = "multi-sleeve live order loop"
            action = "already_running_heartbeat"
            pid = $pidValue
            heartbeat = $heartbeat
        }
    }
    Write-SafeStartLog "multi-sleeve live order loop heartbeat not healthy reason=$($heartbeat.reason); starting loop"
    $sleeveArg = ($SleeveIds -join ",")
    return Start-StackProcess `
        -Name "multi-sleeve live order loop" `
        -Needles @("leaps_multi_sleeve_live_order_loop.ps1", $Config) `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools\leaps_multi_sleeve_live_order_loop.ps1",
            "-Config", $Config,
            "-SleeveIds", $sleeveArg,
            "-IntervalSeconds", "10",
            "-OrderBatchOutput", "data/runtime/live-order-loop/multi_sleeve_candidate_orders.json",
            "-Journal", "data/cycle-journal/live_multi_sleeve.jsonl",
            "-LogPath", "data/runtime/live-order-loop/multi_sleeve.log",
            "-FrameworkStateDir", "data/runtime/framework-state/multi-sleeve",
            "-RuntimeStatePath", "data/runtime/runtime-state/live_multi_sleeve.sqlite",
            "-SubmitStatePath", "data/runtime/live-order-loop/multi_sleeve_submit_state.json",
            "-ControlQueue", "data/runtime/control/live.jsonl",
            "-ActiveSleevesPath", $ActiveSleevesPath,
            "-HeartbeatPath", $LiveLoopHeartbeatPath,
            "-HotReload", "true",
            "-StaleAfterSeconds", "300",
            "-CancelStaleOpenTickets", "true",
            "-ExpireDayOpenTickets", "true"
        ) `
        -StdoutPath "data/runtime/live-order-loop/multi_sleeve_start_stdout.log" `
        -StderrPath "data/runtime/live-order-loop/multi_sleeve_start_stderr.log" `
        -SkipExistingProcessCheck
}

function Start-PortfolioReportLoop {
    param(
        [string]$SleeveId,
        [string]$MarketScope,
        [string]$AccountStore,
        [string]$Title,
        [string]$LogName
    )
    $heartbeatPath = "data/runtime/portfolio-reports/$LogName.heartbeat.json"
    $heartbeat = Get-HeartbeatStatus -PathValue $heartbeatPath -Component "portfolio_report_loop" -MaxAgeSeconds 180
    if ($heartbeat.ok) {
        Write-SafeStartLog "portfolio report loop $SleeveId healthy by heartbeat age_seconds=$([Math]::Round([double]$heartbeat.age_seconds, 1)) path=$($heartbeat.path)"
        return [ordered]@{
            name = "portfolio report loop $SleeveId"
            action = "already_running_heartbeat"
            pid = $heartbeat.payload.process_id
            heartbeat = $heartbeat
        }
    }
    Write-SafeStartLog "portfolio report loop $SleeveId heartbeat not healthy reason=$($heartbeat.reason); starting loop"
    return Start-StackProcess `
        -Name "portfolio report loop $SleeveId" `
        -Needles @("leaps_portfolio_report_loop.ps1", $Config, $SleeveId) `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "tools\leaps_portfolio_report_loop.ps1",
            "-IntervalSeconds", "60",
            "-Config", $Config,
            "-SleeveId", $SleeveId,
            "-AccountStore", $AccountStore,
            "-Title", $Title,
            "-ReportMode", "latest-target",
            "-LogPath", "data/runtime/portfolio-reports/$LogName.log",
            "-ScheduleMode", "phase",
            "-MarketScope", $MarketScope,
            "-StatePath", "data/runtime/portfolio-reports/$LogName.state.json",
            "-HeartbeatPath", $heartbeatPath
        ) `
        -StdoutPath "data/runtime/portfolio-reports/${LogName}_start_stdout.log" `
        -StderrPath "data/runtime/portfolio-reports/${LogName}_start_stderr.log" `
        -SkipExistingProcessCheck
}

function Start-ReportLoops {
    param([string[]]$SleeveIds)
    $started = @()
    foreach ($sleeveId in $SleeveIds) {
        if ($sleeveId -eq "LEaps") {
            $started += Start-PortfolioReportLoop `
                -SleeveId "LEaps" `
                -MarketScope "domestic" `
                -AccountStore "data/virtual-accounts/kis_domestic.json" `
                -Title "LEaps" `
                -LogName "LEaps_portfolio_report_loop"
        } elseif ($sleeveId -eq "kr-lowvol-defensive") {
            $started += Start-PortfolioReportLoop `
                -SleeveId "kr-lowvol-defensive" `
                -MarketScope "domestic" `
                -AccountStore "data/virtual-accounts/kis_domestic.json" `
                -Title "KR_LOWVOL" `
                -LogName "kr_lowvol_defensive_portfolio_report_loop"
        } elseif ($sleeveId -eq "us_etf_rotation") {
            $started += Start-PortfolioReportLoop `
                -SleeveId "us_etf_rotation" `
                -MarketScope "overseas" `
                -AccountStore "data/virtual-accounts/kis_overseas.json" `
                -Title "US_ETF" `
                -LogName "us_etf_rotation_portfolio_report_loop"
        } elseif ($sleeveId -eq "kr-domestic-4401") {
            $started += Start-PortfolioReportLoop `
                -SleeveId "kr-domestic-4401" `
                -MarketScope "domestic" `
                -AccountStore "data/virtual-accounts/kis_domestic_4401.json" `
                -Title "KR_4401" `
                -LogName "kr_domestic_4401_portfolio_report_loop"
        } elseif ($sleeveId -eq "semiconduct-kor") {
            $started += Start-PortfolioReportLoop `
                -SleeveId "semiconduct-kor" `
                -MarketScope "domestic" `
                -AccountStore "data/virtual-accounts/kis_domestic_4401.json" `
                -Title "SEMICON_KOR" `
                -LogName "semiconduct_kor_portfolio_report_loop"
        } else {
            Write-SafeStartLog "report loop skipped unknown sleeve=$sleeveId"
            $started += [ordered]@{
                name = "portfolio report loop $sleeveId"
                action = "skipped_unknown_sleeve"
                pid = $null
            }
        }
    }
    return @($started)
}

function Start-EodScheduler {
    param([string[]]$SleeveIds)
    $schedules = @()
    if ($SleeveIds -contains "kr-lowvol-defensive") {
        $schedules += "18:05|krx-after-hours|$Config|kr-lowvol-defensive|domestic"
    }
    if ($SleeveIds -contains "LEaps") {
        $schedules += "18:05|krx-after-hours|$Config|LEaps|domestic"
    }
    if ($SleeveIds -contains "kr-domestic-4401") {
        $schedules += "18:05|krx-after-hours|$Config|kr-domestic-4401|domestic"
    }
    if ($SleeveIds -contains "semiconduct-kor") {
        $schedules += "18:05|krx-after-hours|$Config|semiconduct-kor|domestic"
    }
    if ($SleeveIds -contains "us_etf_rotation") {
        $schedules += "06:10|us-after-hours|$Config|us_etf_rotation|overseas"
    }
    if ($schedules.Count -eq 0) {
        Write-SafeStartLog "EOD snapshot scheduler skipped: no known active sleeve schedule"
        return [ordered]@{
            name = "EOD snapshot scheduler"
            action = "skipped_no_known_schedules"
            pid = $null
        }
    }
    $heartbeatPath = "data/runtime/eod-snapshots/eod_snapshot_scheduler_heartbeat.json"
    $heartbeat = Get-HeartbeatStatus -PathValue $heartbeatPath -Component "eod_snapshot_scheduler" -MaxAgeSeconds 180
    if ($heartbeat.ok) {
        Write-SafeStartLog "EOD snapshot scheduler healthy by heartbeat age_seconds=$([Math]::Round([double]$heartbeat.age_seconds, 1)) path=$($heartbeat.path)"
        return [ordered]@{
            name = "EOD snapshot scheduler"
            action = "already_running_heartbeat"
            pid = $heartbeat.payload.process_id
            heartbeat = $heartbeat
        }
    }
    Write-SafeStartLog "EOD snapshot scheduler heartbeat not healthy reason=$($heartbeat.reason); starting scheduler"
    $quotedSchedules = @($schedules | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ","
    $command = "& 'tools\leaps_eod_snapshot_scheduler.ps1' -Schedules @($quotedSchedules) -SnapshotRoot 'data/eod-snapshots' -StateDir 'data/runtime/eod-snapshots' -LogPath 'data/runtime/eod-snapshots/eod_snapshot_scheduler.log' -HeartbeatPath '$heartbeatPath' -RetentionDays 31 -CheckIntervalSeconds 60 -WindowMinutes 20"
    return Start-StackProcess `
        -Name "EOD snapshot scheduler" `
        -Needles @("leaps_eod_snapshot_scheduler.ps1") `
        -Arguments @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", $command
        ) `
        -StdoutPath "data/runtime/eod-snapshots/eod_scheduler_start_stdout.log" `
        -StderrPath "data/runtime/eod-snapshots/eod_scheduler_start_stderr.log" `
        -SkipExistingProcessCheck
}

function Get-ProcessSummary {
    $items = @()
    foreach ($spec in @(
        @("kis_gateway", @("kis-gateway-serve")),
        @("broker_engine", @("broker_engine.app")),
        @("multi_sleeve_live_loop", @("leaps_multi_sleeve_live_order_loop.ps1")),
        @("portfolio_report_loop", @("leaps_portfolio_report_loop.ps1")),
        @("eod_snapshot_scheduler", @("leaps_eod_snapshot_scheduler.ps1"))
    )) {
        $name = $spec[0]
        $needles = [string[]]$spec[1]
        foreach ($process in @(Find-ProcessesByCommand -Needles $needles)) {
            $items += [ordered]@{
                name = $name
                pid = $process.ProcessId
                process_name = $process.Name
                command_line = $process.CommandLine
            }
        }
    }
    return @($items)
}

$includeGatewayEnabled = Test-FlagEnabled $IncludeGateway
$includeBrokerEnabled = Test-FlagEnabled $IncludeBroker
$runPreflightEnabled = Test-FlagEnabled $RunPreflight
$includeLiveLoopEnabled = Test-FlagEnabled $IncludeLiveLoop
$includeReportsEnabled = Test-FlagEnabled $IncludeReports
$includeEodSchedulerEnabled = Test-FlagEnabled $IncludeEodScheduler
$dryRunEnabled = Test-FlagEnabled $DryRun

$activeSleeves = @(Get-ActiveSleeveIds)
Write-SafeStartLog "safe-start begin config=$Config active_sleeves=$($activeSleeves -join ',') dry_run=$dryRunEnabled"

$actions = @()
if ($includeGatewayEnabled) {
    $actions += [ordered]@{ component = "kis_gateway"; result = Start-KisGatewayIfNeeded }
}
if ($includeBrokerEnabled) {
    $actions += [ordered]@{ component = "broker_engine"; result = Start-BrokerEngineIfNeeded }
}

$preflight = $null
if ($runPreflightEnabled) {
    $preflight = Invoke-LivePreflight -SleeveIds $activeSleeves
}

$blockedReasons = @()
if ($preflight) {
    $blockedReasons += @($preflight.blocked_reasons)
}

if ($includeLiveLoopEnabled) {
    if ($blockedReasons.Count -gt 0) {
        Write-SafeStartLog "multi-sleeve live order loop blocked reasons=$($blockedReasons -join ',')"
        $actions += [ordered]@{
            component = "multi_sleeve_live_loop"
            result = [ordered]@{
                name = "multi-sleeve live order loop"
                action = "blocked"
                pid = $null
                blocked_reasons = @($blockedReasons)
            }
        }
    } else {
        $actions += [ordered]@{ component = "multi_sleeve_live_loop"; result = Start-MultiSleeveLoop -SleeveIds $activeSleeves }
    }
}

if ($includeReportsEnabled) {
    $actions += [ordered]@{ component = "portfolio_report_loops"; result = @(Start-ReportLoops -SleeveIds $activeSleeves) }
}

if ($includeEodSchedulerEnabled) {
    $actions += [ordered]@{ component = "eod_snapshot_scheduler"; result = Start-EodScheduler -SleeveIds $activeSleeves }
}

if ($VerifySeconds -gt 0 -and -not $dryRunEnabled) {
    Write-SafeStartLog "verify sleep seconds=$VerifySeconds"
    Start-Sleep -Seconds $VerifySeconds
}

$finalGatewayHealth = if ($includeGatewayEnabled) { Invoke-Health -Name "kis_gateway" -Url $GatewayUrl } else { $null }
$finalBrokerHealth = if ($includeBrokerEnabled) { Invoke-Health -Name "broker_engine" -Url $BrokerUrl } else { $null }
$liveLoopHeartbeat = Get-LiveLoopHeartbeat

$status = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    config = $Config
    active_sleeve_ids = @($activeSleeves)
    dry_run = $dryRunEnabled
    process_scan_enabled = (Test-FlagEnabled $UseProcessScan)
    blocked_reasons = @($blockedReasons)
    gateway_health = $finalGatewayHealth
    broker_health = $finalBrokerHealth
    live_loop_heartbeat = $liveLoopHeartbeat
    preflight = if ($preflight) {
        [ordered]@{
            status = $preflight.status
            exit_code = $preflight.exit_code
            blocked_reasons = @($preflight.blocked_reasons)
        }
    } else {
        $null
    }
    actions = @($actions)
    processes = @()
}

$status | ConvertTo-Json -Depth 12 | Set-Content -Path $statusFullPath -Encoding UTF8
Write-SafeStartLog "safe-start end status_path=$statusFullPath process_scan_enabled=$($status.process_scan_enabled) live_loop_heartbeat_ok=$($liveLoopHeartbeat.ok) blocked=$($blockedReasons -join ',')"
$status | ConvertTo-Json -Depth 12
