param(
    [string]$DecisionDate = "",
    [string]$TargetPath = "data/operator-targets/semiconduct-kor/latest_target.json",
    [string]$LogPath = "data/runtime/agent-targets/semiconduct_kor_target_builder.log",
    [string]$HeartbeatPath = "data/runtime/agent-targets/semiconduct_kor_target_builder_heartbeat.json",
    [double]$Cash = 5000000
)

$ErrorActionPreference = "Stop"

function Write-TargetLog {
    param([string]$Message)
    $dir = Split-Path -Parent $LogPath
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $line = "{0} {1}" -f (Get-Date).ToString("o"), $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Write-TargetHeartbeat {
    param(
        [string]$Status,
        [hashtable]$Metadata = @{}
    )
    $dir = Split-Path -Parent $HeartbeatPath
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $payload = [ordered]@{
        schema_version = "runtime_heartbeat.v1"
        runtime_id = "live_multi_sleeve"
        component = "semiconduct_kor_agent_target_builder"
        status = $Status
        updated_at = (Get-Date).ToString("o")
        metadata = $Metadata
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -Path $HeartbeatPath -Encoding UTF8
}

if ([string]::IsNullOrWhiteSpace($DecisionDate)) {
    $DecisionDate = (Get-Date).ToString("yyyy-MM-dd")
}

try {
    Write-TargetLog "start decision_date=$DecisionDate target=$TargetPath"
    Write-TargetHeartbeat -Status "running" -Metadata @{ decision_date = $DecisionDate; target_path = $TargetPath }
    $env:PYTHONPATH = "src"
    $args = @(
        "scripts/research/build_semiconduct_kor_narrative_targets.py",
        "--decision-date", $DecisionDate,
        "--cash", ([string]$Cash),
        "--latest-target-path", $TargetPath,
        "--current-state-path", "sleeves/semiconduct-kor/agent_state/current_state.json",
        "--daily-judgments-dir", "sleeves/semiconduct-kor/agent_state/daily_judgments",
        "--strategy-doc-path", "sleeves/semiconduct-kor/STRATEGY.md",
        "--refresh-news"
    )
    & py -3 @args 2>&1 | ForEach-Object { Write-TargetLog ([string]$_) }
    if ($LASTEXITCODE -ne 0) {
        throw "target builder failed exit=$LASTEXITCODE"
    }
    Write-TargetHeartbeat -Status "ok" -Metadata @{ decision_date = $DecisionDate; target_path = $TargetPath }
    Write-TargetLog "ok decision_date=$DecisionDate target=$TargetPath"
} catch {
    Write-TargetHeartbeat -Status "failed" -Metadata @{ decision_date = $DecisionDate; target_path = $TargetPath; error = $_.Exception.Message }
    Write-TargetLog "failed decision_date=$DecisionDate error=$($_.Exception.Message)"
    throw
}
