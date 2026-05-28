param(
    [string[]]$Targets = @(
        "configs/runtime/live_multi_sleeve.json|LEaps|domestic",
        "configs/runtime/live_multi_sleeve.json|kr-lowvol-defensive|domestic",
        "configs/runtime/live_multi_sleeve.json|us_etf_rotation|overseas"
    ),
    [string]$SnapshotRoot = "data/eod-snapshots",
    [string]$Label = "manual",
    [int]$RetentionDays = 31,
    [switch]$Notify
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root
$env:PYTHONPATH = "src"

function Resolve-RepoPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return (Join-Path $Root $Path)
}

function ConvertTo-SafeName {
    param([string]$Value)
    $text = [string]$Value
    foreach ($char in [System.IO.Path]::GetInvalidFileNameChars()) {
        $text = $text.Replace($char, "_")
    }
    return ($text -replace '[\\/:*?"<>|\s]+', '_')
}

function Parse-Target {
    param([string]$Raw)
    $parts = $Raw.Split("|")
    if ($parts.Count -lt 2) {
        throw "Target must be 'config|sleeve_id' or 'config|sleeve_id|label': $Raw"
    }
    $targetLabel = if ($parts.Count -ge 3 -and $parts[2]) { $parts[2] } else { $parts[1] }
    return [ordered]@{
        config = $parts[0]
        sleeve_id = $parts[1]
        label = $targetLabel
        raw = $Raw
    }
}

function Invoke-CapturedCommand {
    param(
        [string[]]$Arguments,
        [string]$OutputPath,
        [string]$Description
    )
    $started = Get-Date
    $output = py @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $output | Out-File -FilePath $OutputPath -Encoding utf8
    $completed = Get-Date
    return [ordered]@{
        description = $Description
        command = "py " + ($Arguments -join " ")
        exit_code = $exitCode
        output_path = $OutputPath
        started_at = $started.ToString("o")
        completed_at = $completed.ToString("o")
    }
}

function Copy-IfExists {
    param(
        [string]$Source,
        [string]$Destination
    )
    if (Test-Path $Source) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
        return $true
    }
    return $false
}

function Remove-OldSnapshots {
    param(
        [string]$RootPath,
        [int]$Days
    )
    if ($Days -le 0 -or -not (Test-Path $RootPath)) {
        return @()
    }
    $resolvedRoot = (Resolve-Path $RootPath).Path
    $cutoff = (Get-Date).Date.AddDays(-1 * $Days)
    $removed = @()
    foreach ($dir in Get-ChildItem -LiteralPath $resolvedRoot -Directory) {
        [datetime]$parsed = [datetime]::MinValue
        $ok = [datetime]::TryParseExact(
            $dir.Name,
            "yyyy-MM-dd",
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::None,
            [ref]$parsed
        )
        if (-not $ok -or $parsed -ge $cutoff) {
            continue
        }
        $full = (Resolve-Path $dir.FullName).Path
        if ($full.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $full -Recurse -Force
            $removed += $full
        }
    }
    return $removed
}

$snapshotRootFull = Resolve-RepoPath $SnapshotRoot
$snapshotDate = Get-Date -Format "yyyy-MM-dd"
$snapshotTime = Get-Date -Format "yyyyMMdd_HHmmss"
$safeLabel = ConvertTo-SafeName $Label
$runDir = Join-Path $snapshotRootFull (Join-Path $snapshotDate $safeLabel)
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$manifest = [ordered]@{
    schema_version = "leaps_eod_snapshot.v1"
    generated_at = (Get-Date).ToString("o")
    snapshot_date = $snapshotDate
    label = $Label
    retention_days = $RetentionDays
    root = $snapshotRootFull
    targets = @()
    copied_sources = @()
    removed_old_snapshots = @()
}

$exitCode = 0
foreach ($rawTarget in $Targets) {
    try {
        $target = Parse-Target $rawTarget
        $targetDirName = (ConvertTo-SafeName "$($target.label)_$($target.sleeve_id)")
        $targetDir = Join-Path $runDir $targetDirName
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

        $reportDir = Join-Path $targetDir "portfolio-report"
        New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

        $reportArgs = @(
            "-3", "tools/leaps_portfolio_report.py",
            "--config", $target.config,
            "--sleeve-id", $target.sleeve_id,
            "--out-dir", $reportDir
        )
        if ($Notify) {
            $reportArgs += "--notify"
        }
        $reportCapture = Invoke-CapturedCommand `
            -Arguments $reportArgs `
            -OutputPath (Join-Path $targetDir "portfolio_report_stdout.txt") `
            -Description "portfolio_report"

        $orderStatusCapture = Invoke-CapturedCommand `
            -Arguments @(
                "-3", "-m", "leaps_quant_engine.cli", "order-runtime-status",
                $target.config,
                "--sleeve-id", $target.sleeve_id,
                "--recent-events", "80",
                "--summary-only"
            ) `
            -OutputPath (Join-Path $targetDir "order_runtime_status.json") `
            -Description "order_runtime_status"

        $healthCapture = Invoke-CapturedCommand `
            -Arguments @(
                "-3", "-m", "leaps_quant_engine.cli", "runtime-health",
                $target.config,
                "--sleeve-id", $target.sleeve_id,
                "--summary-only"
            ) `
            -OutputPath (Join-Path $targetDir "runtime_health.json") `
            -Description "runtime_health"

        $preflightCapture = Invoke-CapturedCommand `
            -Arguments @(
                "-3", "-m", "leaps_quant_engine.cli", "runtime-preflight",
                $target.config,
                "--sleeve-id", $target.sleeve_id
            ) `
            -OutputPath (Join-Path $targetDir "runtime_preflight.json") `
            -Description "runtime_preflight"

        $targetEntry = [ordered]@{
            raw = $target.raw
            config = $target.config
            sleeve_id = $target.sleeve_id
            label = $target.label
            directory = $targetDir
            commands = @($reportCapture, $orderStatusCapture, $healthCapture, $preflightCapture)
        }
        $manifest.targets += $targetEntry
        foreach ($command in $targetEntry.commands) {
            if ([int]$command.exit_code -ne 0) {
                $exitCode = 1
            }
        }
    } catch {
        $exitCode = 1
        $manifest.targets += [ordered]@{
            raw = $rawTarget
            error = $_.Exception.Message
        }
    }
}

$copiesDir = Join-Path $runDir "stores"
New-Item -ItemType Directory -Force -Path $copiesDir | Out-Null
foreach ($source in @(
    "data/virtual-accounts/kis_domestic.json",
    "data/virtual-accounts/kis_domestic_4401.json",
    "data/virtual-accounts/kis_overseas.json",
    "data/order-runtime/kis_domestic.jsonl",
    "data/order-runtime/kis_domestic_4401.jsonl",
    "data/order-runtime/kis_overseas.jsonl"
)) {
    $sourceFull = Resolve-RepoPath $source
    $dest = Join-Path $copiesDir (Split-Path $source -Leaf)
    if (Copy-IfExists -Source $sourceFull -Destination $dest) {
        $manifest.copied_sources += [ordered]@{
            source = $sourceFull
            destination = $dest
        }
    }
}

$removed = Remove-OldSnapshots -RootPath $snapshotRootFull -Days $RetentionDays
$manifest.removed_old_snapshots = @($removed)
$manifestPath = Join-Path $runDir "manifest_$snapshotTime.json"
$manifest | ConvertTo-Json -Depth 12 | Set-Content -Path $manifestPath -Encoding utf8

Write-Output "EOD snapshot complete: $runDir"
Write-Output "Manifest: $manifestPath"
exit $exitCode
