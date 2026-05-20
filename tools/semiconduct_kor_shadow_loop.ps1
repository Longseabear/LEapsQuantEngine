param(
    [string]$Config = "configs/runtime/semiconduct_kor_shadow.json",
    [string]$SleeveId = "semiconduct-kor",
    [int]$IntervalSeconds = 60,
    [double]$MaxSubmitNotional = 1500000,
    [string]$OrderBatchOutput = "data/runtime/semiconduct-kor-shadow/candidate_orders.json",
    [string]$Journal = "data/cycle-journal/semiconduct_kor_shadow.jsonl",
    [string]$LogPath = "data/runtime/semiconduct-kor-shadow/shadow_loop.log",
    [string]$FrameworkState = "data/runtime/framework-state/semiconduct-kor-shadow.json",
    [string]$RuntimeStatePath = "data/runtime/runtime-state/semiconduct_kor_shadow.sqlite",
    [string]$SubmitLatest = "data/runtime/semiconduct-kor-shadow/submit_latest.json",
    [string]$RunLatest = "data/runtime/semiconduct-kor-shadow/runtime_run_latest.json",
    [string]$SuperviseLatest = "data/runtime/semiconduct-kor-shadow/supervise_latest.json"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONPATH = "src"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Resolve-ShadowPath {
    param([string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return (Join-Path $root $PathValue)
}

function Ensure-Parent {
    param([string]$PathValue)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent (Resolve-ShadowPath $PathValue)) | Out-Null
}

function Write-ShadowLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path (Resolve-ShadowPath $LogPath) -Value "[$timestamp] $Message" -Encoding UTF8
}

function Read-OrderCount {
    param([string]$PathValue)
    try {
        $payload = Get-Content -Path (Resolve-ShadowPath $PathValue) -Raw -Encoding UTF8 | ConvertFrom-Json
        return [int]$payload.order_count
    } catch {
        Write-ShadowLog "order batch read failed: $($_.Exception.Message)"
        return 0
    }
}

Ensure-Parent $OrderBatchOutput
Ensure-Parent $Journal
Ensure-Parent $LogPath
Ensure-Parent $FrameworkState
Ensure-Parent $RuntimeStatePath
Ensure-Parent $SubmitLatest
Ensure-Parent $RunLatest
Ensure-Parent $SuperviseLatest

Write-ShadowLog "semiconduct-kor shadow loop started config=$Config sleeve=$SleeveId interval=${IntervalSeconds}s max_submit_notional=$MaxSubmitNotional runtime_state=$RuntimeStatePath broker=paper"

while ($true) {
    try {
        py -3 -m leaps_quant_engine.cli order-runtime-supervise $Config `
            --sleeve-id $SleeveId `
            --broker paper `
            --skip-reconcile `
            --summary-only 2>&1 | Set-Content -Path (Resolve-ShadowPath $SuperviseLatest) -Encoding UTF8
        Write-ShadowLog "pre-cycle paper supervise exit=$LASTEXITCODE"

        py -3 -m leaps_quant_engine.cli runtime-run-once $Config `
            --sleeve-id $SleeveId `
            --order-batch-output (Resolve-ShadowPath $OrderBatchOutput) `
            --journal (Resolve-ShadowPath $Journal) `
            --framework-state (Resolve-ShadowPath $FrameworkState) `
            --runtime-state (Resolve-ShadowPath $RuntimeStatePath) `
            --summary-only 2>&1 | Set-Content -Path (Resolve-ShadowPath $RunLatest) -Encoding UTF8
        $runExit = $LASTEXITCODE
        Write-ShadowLog "runtime-run-once exit=$runExit"

        if ($runExit -eq 0) {
            $orderCount = Read-OrderCount -PathValue $OrderBatchOutput
            if ($orderCount -gt 0) {
                py -3 -m leaps_quant_engine.cli order-runtime-submit $Config (Resolve-ShadowPath $OrderBatchOutput) `
                    --sleeve-id $SleeveId `
                    --broker paper `
                    --commit `
                    --poll-after-submit `
                    --allow-symbol KRX:005930 `
                    --max-submit-notional $MaxSubmitNotional `
                    --summary-only 2>&1 | Set-Content -Path (Resolve-ShadowPath $SubmitLatest) -Encoding UTF8
                Write-ShadowLog "paper order-runtime-submit exit=$LASTEXITCODE order_count=$orderCount"
            } else {
                Write-ShadowLog "paper order-runtime-submit skipped: no candidate orders"
            }
        }

        py -3 -m leaps_quant_engine.cli order-runtime-supervise $Config `
            --sleeve-id $SleeveId `
            --broker paper `
            --skip-reconcile `
            --summary-only 2>&1 | Set-Content -Path (Resolve-ShadowPath $SuperviseLatest) -Encoding UTF8
        Write-ShadowLog "post-cycle paper supervise exit=$LASTEXITCODE"
    } catch {
        Write-ShadowLog "cycle exception: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds $IntervalSeconds
}
