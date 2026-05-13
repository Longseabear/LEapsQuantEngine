param(
    [string]$Config,
    [string]$SleeveId,
    [int]$IntervalSeconds = 300,
    [double]$MaxSubmitNotional = 2500,
    [string]$OrderBatchOutput = "data/runtime/live_candidate_orders.json",
    [string]$Journal = "data/cycle-journal/live_order_loop.jsonl",
    [string]$LogPath = "data/runtime/live-order-loop/live_order_loop.log",
    [string]$SkipReconcile = "true",
    [int]$ReconcileEveryCycles = 5,
    [string]$FrameworkStatePath = "",
    [string]$SubmitStatePath = "",
    [string]$SubmitOncePerDay = "true"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONPATH = "src"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$logFullPath = Join-Path $root $LogPath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFullPath) | Out-Null
$orderBatchFullPath = if ([System.IO.Path]::IsPathRooted($OrderBatchOutput)) {
    $OrderBatchOutput
} else {
    Join-Path $root $OrderBatchOutput
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $orderBatchFullPath) | Out-Null
$journalFullPath = if ([System.IO.Path]::IsPathRooted($Journal)) {
    $Journal
} else {
    Join-Path $root $Journal
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $journalFullPath) | Out-Null
$submitStateFullPath = $null
if ($SubmitStatePath) {
    $submitStateFullPath = Join-Path $root $SubmitStatePath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $submitStateFullPath) | Out-Null
}
$frameworkStateRelativePath = $FrameworkStatePath
if (-not $frameworkStateRelativePath) {
    $safeSleeveId = $SleeveId -replace '[^A-Za-z0-9._-]', '_'
    $frameworkStateRelativePath = "data/runtime/framework-state/$safeSleeveId.json"
}
$frameworkStateFullPath = if ([System.IO.Path]::IsPathRooted($frameworkStateRelativePath)) {
    $frameworkStateRelativePath
} else {
    Join-Path $root $frameworkStateRelativePath
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $frameworkStateFullPath) | Out-Null

function Write-LoopLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFullPath -Value "[$timestamp] $Message" -Encoding UTF8
}

function Get-OrderBatchHash {
    param([object]$OrderBatch)
    if ($null -eq $OrderBatch -or $null -eq $OrderBatch.batches) {
        return ""
    }
    $ordersJson = ($OrderBatch.batches | ConvertTo-Json -Depth 32 -Compress)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($ordersJson)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha256.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
}

function Test-SubmitGuardBlocked {
    param([string]$BatchHash)
    if (-not $submitStateFullPath -or -not (Test-Path $submitStateFullPath)) {
        return $false
    }
    try {
        $state = Get-Content -Path $submitStateFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-LoopLog "submit state read failed, continuing without block: $($_.Exception.Message)"
        return $false
    }
    if ($state.batch_hash -eq $BatchHash) {
        Write-LoopLog "submit guard blocked: identical order batch already submitted state=$submitStateFullPath trade_date=$($state.trade_date)"
        return $true
    }
    return $false
}

function Save-SubmitState {
    param([string]$Today, [string]$BatchHash, [int]$OrderCount)
    if (-not $submitStateFullPath) {
        return
    }
    $state = [ordered]@{
        trade_date = $Today
        submitted_at = (Get-Date).ToString("o")
        sleeve_id = $SleeveId
        config = $Config
        order_count = $OrderCount
        batch_hash = $BatchHash
        order_batch_output = $OrderBatchOutput
        guard_mode = "engine_target_lineage"
        legacy_submit_once_per_day = $SubmitOncePerDay
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
    $superviseArgs = @(
        "-3", "-m", "leaps_quant_engine.cli", "order-runtime-supervise", $Config,
        "--sleeve-id", $SleeveId,
        "--broker", "broker-engine",
        "--summary-only"
    )
    if ($skipReconcileEnabled -and -not $AllowReconcile) {
        $superviseArgs += "--skip-reconcile"
    }
    if ($Notify) {
        $superviseArgs += "--notify"
    }
    py @superviseArgs 2>&1 | Out-File -FilePath $logFullPath -Append -Encoding utf8
    Write-LoopLog "$Phase order-runtime-supervise exit=$LASTEXITCODE"
}

Write-LoopLog "live order loop started config=$Config sleeve=$SleeveId interval=${IntervalSeconds}s max_notional=$MaxSubmitNotional submit_state=$SubmitStatePath framework_state=$frameworkStateRelativePath submit_once_per_day=$SubmitOncePerDay guard_mode=engine_target_lineage reconcile_every_cycles=$ReconcileEveryCycles"
Write-LoopLog "submit guard note: date-level buy block is disabled; order-runtime-submit uses target_quantity/open_ticket/fill state guards"
Write-LoopLog "resolved paths order_batch=$orderBatchFullPath journal=$journalFullPath log=$logFullPath"
$skipReconcileEnabled = $SkipReconcile -notmatch '^(false|0|no)$'
$cycleIndex = 0

while ($true) {
    try {
        $cycleIndex += 1
        Write-LoopLog "cycle begin"
        $notifyThisCycle = $false
        $reconcileThisCycle = ($ReconcileEveryCycles -gt 0 -and (($cycleIndex % $ReconcileEveryCycles) -eq 0))
        Invoke-OrderRuntimeSupervise -Phase "pre-cycle"

        py -3 -m leaps_quant_engine.cli runtime-run-once $Config `
            --sleeve-id $SleeveId `
            --journal $journalFullPath `
            --order-batch-output $orderBatchFullPath `
            --framework-state $frameworkStateFullPath `
            2>&1 | Out-File -FilePath $logFullPath -Append -Encoding utf8
        $runExit = $LASTEXITCODE
        Write-LoopLog "runtime-run-once exit=$runExit"

        if ($runExit -eq 0) {
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
            } elseif (Test-SubmitGuardBlocked -BatchHash $batchHash) {
                Write-LoopLog "order-runtime-submit skipped by submit guard order_count=$orderCount batch_hash=$batchHash"
            } else {
                $submitArgs = @(
                    "-3", "-m", "leaps_quant_engine.cli", "order-runtime-submit", $Config, $orderBatchFullPath,
                    "--sleeve-id", $SleeveId,
                    "--broker", "broker-engine",
                    "--commit",
                    "--confirm-live-submit",
                    "--summary-only",
                    "--max-submit-notional", $MaxSubmitNotional,
                    "--poll-after-submit"
                )
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
                    Save-SubmitState -Today $today -BatchHash $batchHash -OrderCount $orderCount
                    Write-LoopLog "submit state saved state=$submitStateFullPath order_count=$orderCount batch_hash=$batchHash"
                } elseif ($orderCount -gt 0) {
                    Write-LoopLog "submit state not saved status=$submitStatus order_count=$orderCount batch_hash=$batchHash"
                }
            }
        }

        Invoke-OrderRuntimeSupervise -Phase "post-cycle" -Notify $notifyThisCycle -AllowReconcile ($notifyThisCycle -or $reconcileThisCycle)
        Write-LoopLog "cycle end"
    } catch {
        Write-LoopLog "cycle exception: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
