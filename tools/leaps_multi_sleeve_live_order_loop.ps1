param(
    [string]$Config = "configs/runtime/live_multi_sleeve.json",
    [string[]]$SleeveIds = @("LEaps", "us_etf_rotation"),
    [int]$IntervalSeconds = 60,
    [double]$DomesticMaxSubmitNotional = 12000000,
    [double]$OverseasMaxSubmitNotional = 2500,
    [string]$OrderBatchOutput = "data/runtime/live-order-loop/multi_sleeve_candidate_orders.json",
    [string]$Journal = "data/cycle-journal/live_multi_sleeve.jsonl",
    [string]$LogPath = "data/runtime/live-order-loop/multi_sleeve.log",
    [string]$FrameworkStateDir = "data/runtime/framework-state/multi-sleeve",
    [string]$RuntimeStatePath = "data/runtime/runtime-state/live_multi_sleeve.sqlite",
    [string]$SubmitStatePath = "data/runtime/live-order-loop/multi_sleeve_submit_state.json",
    [string]$ControlQueue = "data/runtime/control/live.jsonl",
    [string]$ActiveSleevesPath = "data/runtime/live-order-loop/multi_sleeve_active_sleeves.json",
    [string]$HotReload = "true",
    [string[]]$SleeveSchedules = @(
        "LEaps|Korea Standard Time|08:30-18:30",
        "us_etf_rotation|Eastern Standard Time|09:30-16:00"
    ),
    [string]$SkipReconcile = "true",
    [int]$ReconcileEveryCycles = 5,
    [double]$StaleAfterSeconds = 300,
    [string]$CancelStaleOpenTickets = "true",
    [string]$ExpireDayOpenTickets = "true"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONPATH = "src"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Resolve-LoopPath {
    param([string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return (Join-Path $root $PathValue)
}

function Normalize-SleeveIds {
    param([object[]]$Items)
    $seen = @{}
    $normalized = @()
    foreach ($item in $Items) {
        foreach ($sleeveId in ([string]$item -split ",")) {
            $trimmed = $sleeveId.Trim()
            if ($trimmed -and -not $seen.ContainsKey($trimmed)) {
                $seen[$trimmed] = $true
                $normalized += $trimmed
            }
        }
    }
    return @($normalized)
}

$script:Config = $Config
$script:SleeveIds = @(Normalize-SleeveIds -Items $SleeveIds)
$script:Paused = $false
$script:ShutdownRequested = $false
$hotReloadEnabled = $HotReload -notmatch '^(false|0|no)$'

$logFullPath = Resolve-LoopPath $LogPath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFullPath) | Out-Null
$orderBatchFullPath = Resolve-LoopPath $OrderBatchOutput
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $orderBatchFullPath) | Out-Null
$journalFullPath = Resolve-LoopPath $Journal
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $journalFullPath) | Out-Null
$frameworkStateDirFullPath = Resolve-LoopPath $FrameworkStateDir
New-Item -ItemType Directory -Force -Path $frameworkStateDirFullPath | Out-Null
$runtimeStateFullPath = Resolve-LoopPath $RuntimeStatePath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $runtimeStateFullPath) | Out-Null
$submitStateFullPath = Resolve-LoopPath $SubmitStatePath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $submitStateFullPath) | Out-Null
$controlQueueFullPath = Resolve-LoopPath $ControlQueue
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $controlQueueFullPath) | Out-Null
$activeSleevesFullPath = Resolve-LoopPath $ActiveSleevesPath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $activeSleevesFullPath) | Out-Null

function Write-LoopLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFullPath -Value "[$timestamp] $Message" -Encoding UTF8
}

function Test-FlagEnabled {
    param([string]$Value)
    return $Value -notmatch '^(false|0|no)$'
}

function Add-SleeveArgs {
    param(
        [string[]]$BaseArgs,
        [string[]]$SleeveIdsForArgs = $script:SleeveIds
    )
    $args = @($BaseArgs)
    foreach ($sleeveId in $SleeveIdsForArgs) {
        $args += @("--sleeve-id", $sleeveId)
    }
    return $args
}

function Parse-SleeveScheduleSpec {
    param([string]$Spec)
    $text = [string]$Spec
    if (-not $text.Trim()) {
        return $null
    }
    $parts = $text -split '\|', 3
    if ($parts.Count -lt 3) {
        Write-LoopLog "schedule spec ignored: expected sleeve|timezone|windows value=$text"
        return $null
    }
    return [pscustomobject]@{
        sleeve_id = $parts[0].Trim()
        timezone = $parts[1].Trim()
        windows = @($parts[2].Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
}

function Get-SleeveSchedule {
    param([string]$SleeveId)
    foreach ($spec in $SleeveSchedules) {
        $schedule = Parse-SleeveScheduleSpec -Spec $spec
        if ($null -ne $schedule -and $schedule.sleeve_id -eq $SleeveId) {
            return $schedule
        }
    }
    return $null
}

function Test-TimeWindow {
    param(
        [TimeSpan]$NowTime,
        [string]$Window
    )
    $windowText = [string]$Window
    if ($windowText -match '^(always|all)$') {
        return $true
    }
    $parts = $windowText -split '-', 2
    if ($parts.Count -ne 2) {
        Write-LoopLog "schedule window ignored: expected HH:mm-HH:mm value=$windowText"
        return $false
    }
    try {
        $start = [TimeSpan]::Parse($parts[0].Trim())
        $end = [TimeSpan]::Parse($parts[1].Trim())
    } catch {
        Write-LoopLog "schedule window parse failed value=$windowText error=$($_.Exception.Message)"
        return $false
    }
    if ($end -gt $start) {
        return ($NowTime -ge $start -and $NowTime -lt $end)
    }
    return ($NowTime -ge $start -or $NowTime -lt $end)
}

function Test-SleeveScheduledNow {
    param([string]$SleeveId)
    $schedule = Get-SleeveSchedule -SleeveId $SleeveId
    if ($null -eq $schedule) {
        return [pscustomobject]@{
            active = $true
            reason = "no_schedule_runs_always"
            market_time = ""
        }
    }
    if ($schedule.timezone -match '^(always|all)$') {
        return [pscustomobject]@{
            active = $true
            reason = "always"
            market_time = ""
        }
    }
    try {
        $zone = [System.TimeZoneInfo]::FindSystemTimeZoneById($schedule.timezone)
        $marketNow = [System.TimeZoneInfo]::ConvertTimeFromUtc((Get-Date).ToUniversalTime(), $zone)
    } catch {
        Write-LoopLog "schedule timezone lookup failed sleeve=$SleeveId timezone=$($schedule.timezone) error=$($_.Exception.Message)"
        return [pscustomobject]@{
            active = $false
            reason = "invalid_timezone"
            market_time = ""
        }
    }
    foreach ($window in @($schedule.windows)) {
        if (Test-TimeWindow -NowTime $marketNow.TimeOfDay -Window $window) {
            return [pscustomobject]@{
                active = $true
                reason = "within_schedule:$window"
                market_time = $marketNow.ToString("yyyy-MM-dd HH:mm:ss")
            }
        }
    }
    return [pscustomobject]@{
        active = $false
        reason = "outside_schedule:$($schedule.windows -join ',')"
        market_time = $marketNow.ToString("yyyy-MM-dd HH:mm:ss")
    }
}

function Get-ScheduledSleeveIds {
    $scheduled = @()
    $skipped = @()
    foreach ($sleeveId in $script:SleeveIds) {
        $status = Test-SleeveScheduledNow -SleeveId $sleeveId
        if ($status.active) {
            $scheduled += $sleeveId
        } else {
            $skipped += "$sleeveId($($status.reason) market_time=$($status.market_time))"
        }
    }
    return [pscustomobject]@{
        sleeve_ids = @($scheduled)
        skipped = @($skipped)
    }
}

function Get-ConfigSleeveIds {
    param([string]$ConfigPath)
    $configFullPath = Resolve-LoopPath $ConfigPath
    if (-not (Test-Path $configFullPath)) {
        throw "config file not found: $configFullPath"
    }
    $payload = Get-Content -Path $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $ids = @()
    foreach ($sleeve in @($payload.sleeves)) {
        $id = [string]$sleeve.sleeve_id
        if (-not $id) {
            $id = [string]$sleeve.id
        }
        if ($id) {
            $ids += $id
        }
    }
    return @($ids)
}

function Test-ActiveSleeveSet {
    param(
        [string]$ConfigPath,
        [string[]]$CandidateSleeveIds
    )
    $candidateIds = @(Normalize-SleeveIds -Items $CandidateSleeveIds)
    if ($candidateIds.Count -eq 0) {
        Write-LoopLog "hot-reload rejected: active sleeve set cannot be empty"
        return $false
    }
    try {
        $configSleeves = @(Get-ConfigSleeveIds -ConfigPath $ConfigPath)
    } catch {
        Write-LoopLog "hot-reload rejected: config sleeve lookup failed config=$ConfigPath error=$($_.Exception.Message)"
        return $false
    }
    foreach ($sleeveId in $candidateIds) {
        if ($configSleeves -notcontains $sleeveId) {
            Write-LoopLog "hot-reload rejected: sleeve=$sleeveId is not present in config=$ConfigPath"
            return $false
        }
    }
    $validateOutput = & py -3 -m leaps_quant_engine.cli runtime-config-validate $ConfigPath 2>&1
    $validateExit = $LASTEXITCODE
    if ($validateExit -ne 0) {
        Write-LoopLog "hot-reload rejected: runtime-config-validate failed config=$ConfigPath exit=$validateExit"
        @($validateOutput | Select-Object -Last 20) | Out-File -FilePath $logFullPath -Append -Encoding utf8
        return $false
    }
    return $true
}

function Save-ActiveSleeveState {
    param([string]$Source)
    $payload = [ordered]@{
        updated_at = (Get-Date).ToString("o")
        source = $Source
        config = $script:Config
        active_sleeve_ids = $script:SleeveIds
        hot_reload = $hotReloadEnabled
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -Path $activeSleevesFullPath -Encoding UTF8
}

function Initialize-ActiveSleeveState {
    if (-not $hotReloadEnabled) {
        return
    }
    if (Test-Path $activeSleevesFullPath) {
        try {
            $state = Get-Content -Path $activeSleevesFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $stateSleeves = @(Normalize-SleeveIds -Items @($state.active_sleeve_ids))
            if ((Test-ActiveSleeveSet -ConfigPath $script:Config -CandidateSleeveIds $stateSleeves)) {
                $script:SleeveIds = $stateSleeves
                Write-LoopLog "hot-reload active sleeve state loaded sleeves=$($script:SleeveIds -join ',') path=$activeSleevesFullPath"
                return
            }
            Write-LoopLog "hot-reload active sleeve state ignored; falling back to startup sleeves"
        } catch {
            Write-LoopLog "hot-reload active sleeve state read failed; falling back to startup sleeves error=$($_.Exception.Message)"
        }
    }
    if (Test-ActiveSleeveSet -ConfigPath $script:Config -CandidateSleeveIds $script:SleeveIds) {
        Save-ActiveSleeveState -Source "startup"
    }
}

function Get-SleeveOperationalState {
    param([string]$SleeveId)
    $statusArgs = @(
        "-3", "-m", "leaps_quant_engine.cli", "order-runtime-status", $script:Config,
        "--sleeve-id", $SleeveId,
        "--summary-only"
    )
    $statusOutput = py @statusArgs 2>&1
    $statusExit = $LASTEXITCODE
    if ($statusExit -ne 0) {
        Write-LoopLog "hot-reload sleeve state lookup failed sleeve=$SleeveId exit=$statusExit"
        @($statusOutput | Select-Object -Last 20) | Out-File -FilePath $logFullPath -Append -Encoding utf8
        return $null
    }
    $text = ($statusOutput | ForEach-Object { [string]$_ }) -join "`n"
    $start = $text.IndexOf("{")
    $end = $text.LastIndexOf("}")
    if ($start -lt 0 -or $end -lt $start) {
        Write-LoopLog "hot-reload sleeve state lookup failed sleeve=$SleeveId reason=no-json"
        return $null
    }
    try {
        $payload = $text.Substring($start, $end - $start + 1) | ConvertFrom-Json
    } catch {
        Write-LoopLog "hot-reload sleeve state parse failed sleeve=$SleeveId error=$($_.Exception.Message)"
        return $null
    }
    $openTickets = 0
    $holdings = 0
    foreach ($route in @($payload.routes)) {
        foreach ($sleeve in @($route.sleeves)) {
            if ([string]$sleeve.sleeve_id -ne $SleeveId) {
                continue
            }
            $openTickets += [int]$sleeve.open_ticket_count
            $holdings += [int]$sleeve.portfolio.holding_count
        }
    }
    return [pscustomobject]@{
        sleeve_id = $SleeveId
        open_ticket_count = $openTickets
        holding_count = $holdings
    }
}

function Test-SleeveCanDeactivate {
    param([string]$SleeveId)
    $state = Get-SleeveOperationalState -SleeveId $SleeveId
    if ($null -eq $state) {
        Write-LoopLog "hot-reload deactivate rejected sleeve=$SleeveId reason=state_unavailable"
        return $false
    }
    if ($state.open_ticket_count -gt 0 -or $state.holding_count -gt 0) {
        Write-LoopLog "hot-reload deactivate rejected sleeve=$SleeveId open_tickets=$($state.open_ticket_count) holdings=$($state.holding_count)"
        return $false
    }
    return $true
}

function Set-ActiveSleeves {
    param(
        [string]$ConfigPath,
        [string[]]$CandidateSleeveIds,
        [string]$Source,
        [bool]$CheckRemovedSleeves = $true
    )
    $candidateIds = @(Normalize-SleeveIds -Items $CandidateSleeveIds)
    if (-not (Test-ActiveSleeveSet -ConfigPath $ConfigPath -CandidateSleeveIds $candidateIds)) {
        return $false
    }
    if ($CheckRemovedSleeves) {
        foreach ($removedSleeveId in @($script:SleeveIds | Where-Object { $candidateIds -notcontains $_ })) {
            if (-not (Test-SleeveCanDeactivate -SleeveId $removedSleeveId)) {
                return $false
            }
        }
    }
    $oldConfig = $script:Config
    $oldSleeves = @($script:SleeveIds)
    $script:Config = $ConfigPath
    $script:SleeveIds = $candidateIds
    Save-ActiveSleeveState -Source $Source
    Write-LoopLog "hot-reload applied source=$Source config=$oldConfig->$script:Config sleeves=$($oldSleeves -join ',')->$($script:SleeveIds -join ',')"
    return $true
}

function Apply-HotReloadControls {
    if (-not $hotReloadEnabled) {
        return
    }
    if (-not (Test-Path $controlQueueFullPath)) {
        return
    }
    $drainOutput = py -3 -m leaps_quant_engine.cli runtime-control-drain --queue $controlQueueFullPath 2>&1
    $drainExit = $LASTEXITCODE
    if ($drainExit -ne 0) {
        Write-LoopLog "hot-reload control drain failed exit=$drainExit"
        @($drainOutput | Select-Object -Last 20) | Out-File -FilePath $logFullPath -Append -Encoding utf8
        return
    }
    $text = ($drainOutput | ForEach-Object { [string]$_ }) -join "`n"
    $start = $text.IndexOf("{")
    $end = $text.LastIndexOf("}")
    if ($start -lt 0 -or $end -lt $start) {
        Write-LoopLog "hot-reload control drain returned no JSON"
        return
    }
    try {
        $payload = $text.Substring($start, $end - $start + 1) | ConvertFrom-Json
    } catch {
        Write-LoopLog "hot-reload control drain parse failed error=$($_.Exception.Message)"
        return
    }
    if ([int]$payload.command_count -le 0) {
        return
    }
    Write-LoopLog "hot-reload control commands received count=$($payload.command_count)"
    foreach ($command in @($payload.commands)) {
        $commandType = [string]$command.command_type
        $commandConfig = [string]$command.payload.config_path
        if (-not $commandConfig) {
            $commandConfig = $script:Config
        }
        $commandSleeveId = [string]$command.payload.sleeve_id
        switch ($commandType) {
            "reload_config" {
                [void](Set-ActiveSleeves -ConfigPath $commandConfig -CandidateSleeveIds $script:SleeveIds -Source "control:reload_config" -CheckRemovedSleeves $false)
            }
            "reload_sleeve" {
                if ($script:SleeveIds -contains $commandSleeveId) {
                    [void](Set-ActiveSleeves -ConfigPath $commandConfig -CandidateSleeveIds $script:SleeveIds -Source "control:reload_sleeve:$commandSleeveId" -CheckRemovedSleeves $false)
                } else {
                    Write-LoopLog "hot-reload reload_sleeve ignored sleeve=$commandSleeveId reason=not_active use=activate_sleeve"
                }
            }
            "activate_sleeve" {
                $candidate = @($script:SleeveIds + $commandSleeveId)
                [void](Set-ActiveSleeves -ConfigPath $commandConfig -CandidateSleeveIds $candidate -Source "control:activate_sleeve:$commandSleeveId" -CheckRemovedSleeves $false)
            }
            "deactivate_sleeve" {
                $candidate = @($script:SleeveIds | Where-Object { $_ -ne $commandSleeveId })
                [void](Set-ActiveSleeves -ConfigPath $commandConfig -CandidateSleeveIds $candidate -Source "control:deactivate_sleeve:$commandSleeveId" -CheckRemovedSleeves $true)
            }
            "pause_worker" {
                $script:Paused = $true
                Write-LoopLog "hot-reload applied pause_worker"
            }
            "resume_worker" {
                $script:Paused = $false
                Write-LoopLog "hot-reload applied resume_worker"
            }
            "run_once" {
                Write-LoopLog "hot-reload run_once requested; next cycle continues immediately"
            }
            "shutdown" {
                $script:ShutdownRequested = $true
                Write-LoopLog "hot-reload shutdown requested"
            }
            default {
                Write-LoopLog "hot-reload ignored unsupported command=$commandType"
            }
        }
    }
}

function Get-OrderBatchHash {
    param([object]$OrderBatch)
    if ($null -eq $OrderBatch -or $null -eq $OrderBatch.batches) {
        return ""
    }
    $signatures = @()
    foreach ($batch in @($OrderBatch.batches)) {
        foreach ($order in @($batch.orders)) {
            $metadata = $order.metadata
            $signatures += [ordered]@{
                sleeve_id = [string]$order.sleeve_id
                symbol = [string]$order.symbol
                side = [string]$order.side
                quantity = [string]$order.quantity
                reference_price = [string]$order.reference_price
                limit_price = [string]$order.limit_price
                order_type = [string]$order.order_type
                time_in_force = [string]$order.time_in_force
                tag = [string]$order.tag
                current_quantity = [string]$metadata.current_quantity
                target_quantity = [string]$metadata.target_quantity
            }
        }
    }
    if ($signatures.Count -eq 0) {
        return ""
    }
    $stableOrders = $signatures | Sort-Object sleeve_id, symbol, side, quantity, reference_price, limit_price, order_type, time_in_force, tag, current_quantity, target_quantity
    $ordersJson = ($stableOrders | ConvertTo-Json -Depth 16 -Compress)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($ordersJson)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha256.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
}

function Test-SubmitGuardBlocked {
    param([string]$Today, [string]$BatchHash)
    if (-not (Test-Path $submitStateFullPath)) {
        return $false
    }
    try {
        $state = Get-Content -Path $submitStateFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-LoopLog "submit state read failed, continuing without block: $($_.Exception.Message)"
        return $false
    }
    if ($state.trade_date -eq $Today -and $state.batch_hash -eq $BatchHash) {
        Write-LoopLog "submit guard blocked: identical multi-sleeve order batch already submitted state=$submitStateFullPath"
        return $true
    }
    return $false
}

function Save-SubmitState {
    param(
        [string]$Today,
        [string]$BatchHash,
        [int]$OrderCount,
        [string[]]$SubmittedSleeveIds = $script:SleeveIds
    )
    $state = [ordered]@{
        trade_date = $Today
        submitted_at = (Get-Date).ToString("o")
        sleeve_ids = $SubmittedSleeveIds
        config = $script:Config
        order_count = $OrderCount
        batch_hash = $BatchHash
        order_batch_output = $OrderBatchOutput
        guard_mode = "multi_sleeve_engine_target_lineage"
    }
    $state | ConvertTo-Json -Depth 8 | Set-Content -Path $submitStateFullPath -Encoding UTF8
}

function Get-SubmitReportStatus {
    param([object[]]$SubmitOutput)
    if ($null -eq $SubmitOutput -or $SubmitOutput.Count -eq 0) {
        return ""
    }
    $text = ($SubmitOutput | ForEach-Object { [string]$_ }) -join "`n"
    $start = $text.IndexOf("{")
    $end = $text.LastIndexOf("}")
    if ($start -lt 0 -or $end -lt $start) {
        return ""
    }
    try {
        $payload = $text.Substring($start, $end - $start + 1) | ConvertFrom-Json
        return [string]$payload.status
    } catch {
        Write-LoopLog "submit status parse failed: $($_.Exception.Message)"
        return ""
    }
}

function Invoke-OrderRuntimeSupervise {
    param(
        [string]$Phase,
        [bool]$Notify = $false,
        [bool]$AllowReconcile = $false
    )
    $superviseArgs = Add-SleeveArgs @(
        "-3", "-m", "leaps_quant_engine.cli", "order-runtime-supervise", $script:Config,
        "--broker", "broker-engine",
        "--summary-only"
    )
    if ((Test-FlagEnabled -Value $SkipReconcile) -and -not $AllowReconcile) {
        $superviseArgs += "--skip-reconcile"
    }
    if ($Notify) {
        $superviseArgs += "--notify"
    }
    if ($StaleAfterSeconds -gt 0) {
        $superviseArgs += @("--stale-after-seconds", ([string]$StaleAfterSeconds))
    }
    if (Test-FlagEnabled -Value $CancelStaleOpenTickets) {
        $superviseArgs += "--cancel-stale-open-tickets"
    }
    if (Test-FlagEnabled -Value $ExpireDayOpenTickets) {
        $superviseArgs += "--expire-day-open-tickets"
    }
    py @superviseArgs 2>&1 | Out-File -FilePath $logFullPath -Append -Encoding utf8
    Write-LoopLog "$Phase order-runtime-supervise exit=$LASTEXITCODE"
}

Initialize-ActiveSleeveState

Write-LoopLog "multi-sleeve live order loop started config=$script:Config sleeves=$($script:SleeveIds -join ',') interval=${IntervalSeconds}s domestic_max=$DomesticMaxSubmitNotional overseas_max=$OverseasMaxSubmitNotional framework_state_dir=$FrameworkStateDir runtime_state=$runtimeStateFullPath hot_reload=$hotReloadEnabled control_queue=$controlQueueFullPath active_sleeves=$activeSleevesFullPath schedules=$($SleeveSchedules -join ';')"
Write-LoopLog "resolved paths order_batch=$orderBatchFullPath journal=$journalFullPath log=$logFullPath submit_state=$submitStateFullPath"
$cycleIndex = 0

while ($true) {
    try {
        Apply-HotReloadControls
        if ($script:ShutdownRequested) {
            Write-LoopLog "loop shutdown before cycle"
            break
        }
        if ($script:Paused) {
            Write-LoopLog "cycle paused sleeves=$($script:SleeveIds -join ',')"
            Invoke-OrderRuntimeSupervise -Phase "paused-cycle"
            Start-Sleep -Seconds $IntervalSeconds
            continue
        }
        $cycleIndex += 1
        $schedule = Get-ScheduledSleeveIds
        $scheduledSleeveIds = @($schedule.sleeve_ids)
        Write-LoopLog "cycle begin config=$script:Config active_sleeves=$($script:SleeveIds -join ',') scheduled_sleeves=$($scheduledSleeveIds -join ',') skipped=$($schedule.skipped -join ';')"
        $notifyThisCycle = $false
        $reconcileThisCycle = ($ReconcileEveryCycles -gt 0 -and (($cycleIndex % $ReconcileEveryCycles) -eq 0))
        Invoke-OrderRuntimeSupervise -Phase "pre-cycle"

        $runExit = 0
        if ($scheduledSleeveIds.Count -le 0) {
            Write-LoopLog "runtime-run-multi-once skipped: no scheduled sleeves"
        } else {
            $runArgs = Add-SleeveArgs @(
                "-3", "-m", "leaps_quant_engine.cli", "runtime-run-multi-once", $script:Config,
                "--journal", $journalFullPath,
                "--order-batch-output", $orderBatchFullPath,
                "--framework-state-dir", $frameworkStateDirFullPath,
                "--runtime-state", $runtimeStateFullPath,
                "--summary-only"
            ) -SleeveIdsForArgs $scheduledSleeveIds
            py @runArgs 2>&1 | Out-File -FilePath $logFullPath -Append -Encoding utf8
            $runExit = $LASTEXITCODE
            Write-LoopLog "runtime-run-multi-once exit=$runExit sleeves=$($scheduledSleeveIds -join ',')"
        }

        if ($runExit -eq 0 -and $scheduledSleeveIds.Count -gt 0) {
            $orderCount = 0
            $orderBatch = $null
            $batchHash = ""
            $orderBatchReadOk = $false
            try {
                $orderBatch = Get-Content -Path $orderBatchFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $orderCount = [int]($orderBatch.order_count)
                $batchHash = Get-OrderBatchHash -OrderBatch $orderBatch
                $orderBatchReadOk = $true
            } catch {
                Write-LoopLog "order batch count read failed: $($_.Exception.Message)"
            }

            $today = Get-Date -Format "yyyy-MM-dd"
            if (-not $orderBatchReadOk) {
                Write-LoopLog "order-runtime-submit skipped: unreadable order batch artifact path=$orderBatchFullPath"
            } elseif ($orderCount -le 0) {
                Write-LoopLog "order-runtime-submit skipped: no candidate orders"
            } elseif (Test-SubmitGuardBlocked -Today $today -BatchHash $batchHash) {
                Write-LoopLog "order-runtime-submit skipped by submit guard order_count=$orderCount batch_hash=$batchHash"
            } else {
                $submitArgs = Add-SleeveArgs @(
                    "-3", "-m", "leaps_quant_engine.cli", "order-runtime-submit", $script:Config, $orderBatchFullPath,
                    "--broker", "broker-engine",
                    "--commit",
                    "--confirm-live-submit",
                    "--summary-only",
                    "--poll-after-submit",
                    "--max-submit-notional-by-account", "kis-domestic=$DomesticMaxSubmitNotional",
                    "--max-submit-notional-by-account", "domestic=$DomesticMaxSubmitNotional",
                    "--max-submit-notional-by-account", "kis-overseas=$OverseasMaxSubmitNotional",
                    "--max-submit-notional-by-account", "overseas=$OverseasMaxSubmitNotional"
                ) -SleeveIdsForArgs $scheduledSleeveIds
                if ($orderCount -gt 0) {
                    $submitArgs += "--notify"
                    $notifyThisCycle = $true
                }
                $submitOutput = py @submitArgs 2>&1
                $submitOutput | Out-File -FilePath $logFullPath -Append -Encoding utf8
                $submitExit = $LASTEXITCODE
                $submitStatus = Get-SubmitReportStatus -SubmitOutput $submitOutput
                Write-LoopLog "order-runtime-submit exit=$submitExit status=$submitStatus"
                if ($submitExit -eq 0 -and $orderCount -gt 0 -and $submitStatus -in @("submitted", "submitted_with_warnings", "ok")) {
                    Save-SubmitState -Today $today -BatchHash $batchHash -OrderCount $orderCount -SubmittedSleeveIds $scheduledSleeveIds
                    Write-LoopLog "submit state saved order_count=$orderCount batch_hash=$batchHash"
                } elseif ($orderCount -gt 0) {
                    Write-LoopLog "submit state not saved status=$submitStatus order_count=$orderCount batch_hash=$batchHash"
                }
            }
        } elseif ($scheduledSleeveIds.Count -le 0) {
            Write-LoopLog "order-runtime-submit skipped: no scheduled sleeves"
        }

        Invoke-OrderRuntimeSupervise -Phase "post-cycle" -Notify $notifyThisCycle -AllowReconcile ($notifyThisCycle -or $reconcileThisCycle)
        Write-LoopLog "cycle end"
    } catch {
        Write-LoopLog "cycle exception: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
