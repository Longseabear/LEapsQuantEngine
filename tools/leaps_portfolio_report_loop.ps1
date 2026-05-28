param(
    [int]$IntervalSeconds = 60,
    [string]$Config = "configs/runtime/live_multi_sleeve.json",
    [string]$SleeveId = "LEaps",
    [string]$LogPath = "data/runtime/portfolio-reports/leaps_portfolio_report_loop.log",
    [string]$AccountStore = "data/virtual-accounts/kis_domestic.json",
    [string]$Title = "LEaps",
    [ValidateSet("phase", "interval")]
    [string]$ScheduleMode = "phase",
    [ValidateSet("auto", "domestic", "overseas")]
    [string]$MarketScope = "auto",
    [ValidateSet("latest-target", "fast-current", "recompute")]
    [string]$ReportMode = "latest-target",
    [string]$StatePath = "",
    [string]$HeartbeatPath = "",
    [int]$FailureRetrySeconds = 300,
    [int]$IdleLogEverySeconds = 1800
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Resolve-LoopPath {
    param([string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return (Join-Path $Root $PathValue)
}

$logFullPath = Resolve-LoopPath $LogPath
$logDir = Split-Path $logFullPath -Parent
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

if ([string]::IsNullOrWhiteSpace($StatePath)) {
    $logBaseName = [System.IO.Path]::GetFileNameWithoutExtension($LogPath)
    $logParent = Split-Path $LogPath -Parent
    if ([string]::IsNullOrWhiteSpace($logParent)) {
        $StatePath = "$logBaseName.state.json"
    } else {
        $StatePath = Join-Path $logParent "$logBaseName.state.json"
    }
}

$stateFullPath = Resolve-LoopPath $StatePath
New-Item -ItemType Directory -Force -Path (Split-Path $stateFullPath -Parent) | Out-Null
if ([string]::IsNullOrWhiteSpace($HeartbeatPath)) {
    $stateBaseName = [System.IO.Path]::GetFileNameWithoutExtension($StatePath)
    $stateParent = Split-Path $StatePath -Parent
    if ([string]::IsNullOrWhiteSpace($stateParent)) {
        $HeartbeatPath = "$stateBaseName.heartbeat.json"
    } else {
        $HeartbeatPath = Join-Path $stateParent "$stateBaseName.heartbeat.json"
    }
}
$heartbeatFullPath = Resolve-LoopPath $HeartbeatPath
New-Item -ItemType Directory -Force -Path (Split-Path $heartbeatFullPath -Parent) | Out-Null

function Write-LoopLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFullPath -Value "[$timestamp] $Message" -Encoding UTF8
}

function Write-Heartbeat {
    param(
        [string]$Status,
        [string]$Phase,
        [hashtable]$Metadata = @{}
    )
    $payload = [ordered]@{
        schema_version = "runtime_heartbeat.v1"
        runtime_id = "live_multi_sleeve"
        component = "portfolio_report_loop"
        status = $Status
        updated_at = (Get-Date).ToString("o")
        config_path = $Config
        config_version = ""
        sleeve_ids = @($SleeveId)
        cycle_index = $null
        process_id = $PID
        metadata = [ordered]@{
            phase = $Phase
            market_scope = $resolvedMarketScope
            report_mode = $ReportMode
            process_id_liveness_checked = $false
        }
    }
    foreach ($key in $Metadata.Keys) {
        $payload.metadata[$key] = $Metadata[$key]
    }
    try {
        $tmp = "$heartbeatFullPath.tmp"
        $payload | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $tmp -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $heartbeatFullPath -Force
    } catch {
        Write-LoopLog "heartbeat write failed path=$heartbeatFullPath error=$($_.Exception.Message)"
    }
}

function Resolve-MarketScope {
    if ($MarketScope -ne "auto") {
        return $MarketScope
    }
    if ($SleeveId -like "*us*" -or $Config -like "*us_etf*") {
        return "overseas"
    }
    return "domestic"
}

function Get-MarketTimeZone {
    param([string]$Scope)
    $zoneId = if ($Scope -eq "overseas") { "Eastern Standard Time" } else { "Korea Standard Time" }
    try {
        return [System.TimeZoneInfo]::FindSystemTimeZoneById($zoneId)
    } catch {
        Write-LoopLog "timezone lookup failed zone=$zoneId fallback=local error=$($_.Exception.Message)"
        return [System.TimeZoneInfo]::Local
    }
}

function New-ReportPhase {
    param(
        [string]$Name,
        [string]$Label,
        [string]$Start,
        [string]$End
    )
    return [pscustomobject]@{
        name = $Name
        label = $Label
        start = $Start
        end = $End
    }
}

function Get-ReportPhases {
    param([string]$Scope)
    if ($Scope -eq "overseas") {
        return @(
            (New-ReportPhase -Name "pre_market" -Label "US pre-market" -Start "04:00:00" -End "09:30:00"),
            (New-ReportPhase -Name "regular_market" -Label "US regular" -Start "09:30:00" -End "16:00:00"),
            (New-ReportPhase -Name "after_market" -Label "US after-market" -Start "16:00:00" -End "20:00:00")
        )
    }
    return @(
        (New-ReportPhase -Name "pre_market" -Label "KRX pre-market" -Start "08:30:00" -End "09:00:00"),
        (New-ReportPhase -Name "regular_market" -Label "KRX regular" -Start "09:00:00" -End "15:30:00"),
        (New-ReportPhase -Name "after_market" -Label "KRX after-market" -Start "15:40:00" -End "18:30:00")
    )
}

function Get-CurrentReportPhase {
    param(
        [datetime]$MarketNow,
        [object[]]$Phases
    )
    $time = $MarketNow.TimeOfDay
    foreach ($phase in $Phases) {
        $start = [TimeSpan]::Parse($phase.start)
        $end = [TimeSpan]::Parse($phase.end)
        if ($end -gt $start) {
            if ($time -ge $start -and $time -lt $end) {
                return $phase
            }
        } else {
            if ($time -ge $start -or $time -lt $end) {
                return $phase
            }
        }
    }
    return $null
}

function Read-ReportState {
    if (-not (Test-Path $stateFullPath)) {
        return [pscustomobject]@{ sent_phase_keys = @() }
    }
    try {
        return (Get-Content -Path $stateFullPath -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        Write-LoopLog "state read failed, starting empty state path=$stateFullPath error=$($_.Exception.Message)"
        return [pscustomobject]@{ sent_phase_keys = @() }
    }
}

function Get-SentPhaseKeys {
    param([object]$State)
    if (-not $State -or -not $State.sent_phase_keys) {
        return @()
    }
    if ($State.sent_phase_keys -is [System.Array]) {
        return @($State.sent_phase_keys | ForEach-Object { [string]$_ })
    }
    return @([string]$State.sent_phase_keys)
}

function Save-ReportState {
    param(
        [string[]]$SentPhaseKeys,
        [string]$Scope
    )
    $boundedKeys = @($SentPhaseKeys | Select-Object -Last 240)
    $payload = [pscustomobject]@{
        updated_at = (Get-Date).ToString("o")
        market_scope = $Scope
        sent_phase_keys = $boundedKeys
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -Path $stateFullPath -Encoding UTF8
}

function Invoke-PortfolioReport {
    param(
        [string]$PhaseName,
        [string]$PhaseLabel
    )
    $reportTitle = "$Title - $PhaseLabel"
    $started = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-LoopLog "report start phase=$PhaseName label=$PhaseLabel"

    $output = & py -3 tools/leaps_portfolio_report.py --config $Config --sleeve-id $SleeveId --account-store $AccountStore --title $reportTitle --mode $ReportMode --notify 2>&1
    $exitCode = $LASTEXITCODE
    $lines = @($output | ForEach-Object { [string]$_ })
    if ($exitCode -eq 0) {
        $lines | Out-File -FilePath $logFullPath -Append -Encoding utf8
        $completed = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-LoopLog "report complete phase=$PhaseName started_at=$started completed_at=$completed"
        return $true
    }

    Write-LoopLog "report failed phase=$PhaseName exit=$exitCode showing_tail_lines=40"
    $lines | Select-Object -Last 40 | Out-File -FilePath $logFullPath -Append -Encoding utf8
    return $false
}

$resolvedMarketScope = Resolve-MarketScope
$marketTimeZone = Get-MarketTimeZone -Scope $resolvedMarketScope
$phases = Get-ReportPhases -Scope $resolvedMarketScope
$phaseDescription = ($phases | ForEach-Object { "$($_.name)=$($_.start)-$($_.end)" }) -join ","

Write-LoopLog "portfolio report loop started config=$Config sleeve=$SleeveId mode=$ReportMode schedule_mode=$ScheduleMode interval=${IntervalSeconds}s market_scope=$resolvedMarketScope timezone=$($marketTimeZone.Id) state=$StatePath heartbeat=$HeartbeatPath phases=$phaseDescription"
Write-Heartbeat -Status "running" -Phase "started"

$lastIdleLogAt = [datetime]::MinValue
$lastLoggedSentKey = ""

while ($true) {
    Write-Heartbeat -Status "running" -Phase "loop_tick"
    if ($ScheduleMode -eq "interval") {
        $ok = Invoke-PortfolioReport -PhaseName "interval" -PhaseLabel "interval"
        if ($ok) {
            Start-Sleep -Seconds $IntervalSeconds
        } else {
            Start-Sleep -Seconds $FailureRetrySeconds
        }
        continue
    }

    $marketNow = [System.TimeZoneInfo]::ConvertTimeFromUtc((Get-Date).ToUniversalTime(), $marketTimeZone)
    $phase = Get-CurrentReportPhase -MarketNow $marketNow -Phases $phases
    if (-not $phase) {
        $now = Get-Date
        Write-Heartbeat -Status "idle" -Phase "outside_report_phase" -Metadata @{ market_time = $marketNow.ToString("yyyy-MM-dd HH:mm:ss") }
        if (($now - $lastIdleLogAt).TotalSeconds -ge $IdleLogEverySeconds) {
            Write-LoopLog "outside report phase market_time=$($marketNow.ToString('yyyy-MM-dd HH:mm:ss'))"
            $lastIdleLogAt = $now
        }
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    $marketDate = $marketNow.ToString("yyyy-MM-dd")
    $phaseKey = "$marketDate|$($phase.name)"
    $state = Read-ReportState
    $sentKeys = @(Get-SentPhaseKeys -State $state)
    if ($sentKeys -contains $phaseKey) {
        if ($lastLoggedSentKey -ne $phaseKey) {
            Write-LoopLog "report already sent phase_key=$phaseKey market_time=$($marketNow.ToString('yyyy-MM-dd HH:mm:ss'))"
            $lastLoggedSentKey = $phaseKey
        }
        Write-Heartbeat -Status "idle" -Phase "phase_already_sent" -Metadata @{ phase_key = $phaseKey }
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    $ok = Invoke-PortfolioReport -PhaseName $phase.name -PhaseLabel $phase.label
    if ($ok) {
        $sentKeys = @($sentKeys + $phaseKey | Select-Object -Unique)
        Save-ReportState -SentPhaseKeys $sentKeys -Scope $resolvedMarketScope
        $lastLoggedSentKey = $phaseKey
        Write-Heartbeat -Status "running" -Phase "report_sent" -Metadata @{ phase_key = $phaseKey }
        Start-Sleep -Seconds $IntervalSeconds
    } else {
        Write-Heartbeat -Status "error" -Phase "report_failed" -Metadata @{ phase_key = $phaseKey }
        Start-Sleep -Seconds $FailureRetrySeconds
    }
}
