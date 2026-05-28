param(
    [string[]]$Schedules = @(
        "18:05|krx-after-hours|configs/runtime/live_multi_sleeve.json|LEaps|domestic",
        "18:05|krx-after-hours|configs/runtime/live_multi_sleeve.json|kr-lowvol-defensive|domestic",
        "18:05|krx-after-hours|configs/runtime/live_multi_sleeve.json|kr-domestic-4401|domestic",
        "18:05|krx-after-hours|configs/runtime/live_multi_sleeve.json|semiconduct-kor|domestic",
        "06:10|us-after-hours|configs/runtime/live_multi_sleeve.json|us_etf_rotation|overseas"
    ),
    [string]$SnapshotRoot = "data/eod-snapshots",
    [string]$StateDir = "data/runtime/eod-snapshots",
    [string]$LogPath = "data/runtime/eod-snapshots/eod_snapshot_scheduler.log",
    [string]$HeartbeatPath = "data/runtime/eod-snapshots/eod_snapshot_scheduler_heartbeat.json",
    [int]$RetentionDays = 31,
    [int]$CheckIntervalSeconds = 60,
    [int]$WindowMinutes = 20,
    [switch]$Notify
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

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

function Write-Log {
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
        component = "eod_snapshot_scheduler"
        status = $Status
        updated_at = (Get-Date).ToString("o")
        config_path = ""
        config_version = ""
        sleeve_ids = @()
        cycle_index = $null
        process_id = $PID
        metadata = [ordered]@{
            phase = $Phase
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
        Write-Log "heartbeat write failed path=$heartbeatFullPath error=$($_.Exception.Message)"
    }
}

function Parse-Schedule {
    param([string]$Raw)
    $parts = $Raw.Split("|")
    if ($parts.Count -lt 4) {
        throw "Schedule must be 'HH:mm|label|config|sleeve_id' or 'HH:mm|label|config|sleeve_id|target_label': $Raw"
    }
    $targetLabel = if ($parts.Count -ge 5 -and $parts[4]) { $parts[4] } else { $parts[1] }
    return [ordered]@{
        time = $parts[0]
        label = $parts[1]
        config = $parts[2]
        sleeve_id = $parts[3]
        target_label = $targetLabel
        raw = $Raw
    }
}

function Get-MinutesOfDay {
    param([datetime]$Value)
    return ($Value.Hour * 60) + $Value.Minute
}

function Is-Due {
    param(
        [datetime]$Now,
        [string]$TimeText,
        [int]$Window
    )
    $parts = $TimeText.Split(":")
    if ($parts.Count -ne 2) {
        throw "Invalid schedule time: $TimeText"
    }
    $targetMinutes = ([int]$parts[0] * 60) + [int]$parts[1]
    $nowMinutes = Get-MinutesOfDay -Value $Now
    $diff = $nowMinutes - $targetMinutes
    return ($diff -ge 0 -and $diff -lt $Window)
}

$logFullPath = Resolve-RepoPath $LogPath
$stateFullDir = Resolve-RepoPath $StateDir
$heartbeatFullPath = Resolve-RepoPath $HeartbeatPath
$snapshotScript = Resolve-RepoPath "tools/leaps_eod_snapshot.ps1"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFullPath) | Out-Null
New-Item -ItemType Directory -Force -Path $stateFullDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $heartbeatFullPath) | Out-Null

Write-Log "EOD snapshot scheduler started schedules=$($Schedules -join '; ') retention_days=$RetentionDays check_interval=${CheckIntervalSeconds}s window=${WindowMinutes}m notify=$Notify heartbeat=$HeartbeatPath"
Write-Heartbeat -Status "running" -Phase "started"

while ($true) {
    Write-Heartbeat -Status "running" -Phase "loop_tick"
    $now = Get-Date
    $dueByLabel = @{}
    foreach ($rawSchedule in $Schedules) {
        try {
            $schedule = Parse-Schedule $rawSchedule
            if (-not (Is-Due -Now $now -TimeText $schedule.time -Window $WindowMinutes)) {
                continue
            }
            $label = [string]$schedule.label
            if (-not $dueByLabel.ContainsKey($label)) {
                $dueByLabel[$label] = [ordered]@{
                    label = $label
                    time = $schedule.time
                    raw = @()
                    targets = @()
                }
            }
            $dueByLabel[$label].raw += $rawSchedule
            $dueByLabel[$label].targets += ("{0}|{1}|{2}" -f $schedule.config, $schedule.sleeve_id, $schedule.target_label)
        } catch {
            Write-Log "schedule failed raw=$rawSchedule error=$($_.Exception.Message)"
        }
    }

    foreach ($label in $dueByLabel.Keys) {
        try {
            $group = $dueByLabel[$label]
            $dateKey = $now.ToString("yyyy-MM-dd")
            $markerName = "{0}_{1}.done" -f $dateKey, (ConvertTo-SafeName $label)
            $markerPath = Join-Path $stateFullDir $markerName
            if (Test-Path $markerPath) {
                continue
            }

            $args = @(
                "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", $snapshotScript,
                "-Targets"
            )
            foreach ($targetSpec in @($group.targets)) {
                $args += $targetSpec
            }
            $args += @(
                "-SnapshotRoot", $SnapshotRoot,
                "-Label", $label,
                "-RetentionDays", "$RetentionDays"
            )
            if ($Notify) {
                $args += "-Notify"
            }

            $targetSummary = (@($group.targets) -join ";")
            Write-Log "snapshot start label=$label targets=$targetSummary"
            Write-Heartbeat -Status "running" -Phase "snapshot_start" -Metadata @{ label = $label; targets = @($group.targets) }
            $output = powershell.exe @args 2>&1
            $exitCode = $LASTEXITCODE
            $output | Out-File -FilePath $logFullPath -Append -Encoding utf8
            $marker = [ordered]@{
                date = $dateKey
                label = $label
                schedule_time = $group.time
                target = $targetSummary
                targets = @($group.targets)
                attempted_at = (Get-Date).ToString("o")
                exit_code = $exitCode
            }
            $marker | ConvertTo-Json -Depth 6 | Set-Content -Path $markerPath -Encoding utf8
            Write-Log "snapshot complete label=$label exit=$exitCode marker=$markerPath"
            Write-Heartbeat -Status "running" -Phase "snapshot_complete" -Metadata @{ label = $label; exit_code = $exitCode; marker = $markerPath }
        } catch {
            Write-Log "schedule group failed label=$label error=$($_.Exception.Message)"
            Write-Heartbeat -Status "error" -Phase "snapshot_failed" -Metadata @{ label = $label; error = $_.Exception.Message }
        }
    }
    Start-Sleep -Seconds $CheckIntervalSeconds
}
