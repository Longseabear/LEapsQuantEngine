param(
    [string]$Config = "configs/runtime/live_multi_sleeve.json",
    [string[]]$SleeveIds = @("LEaps", "kr-lowvol-defensive", "kr-domestic-4401", "semiconduct-kor", "us_etf_rotation"),
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8876,
    [int]$HttpsPort = 8877,
    [string]$InstallScheduledTask = "false",
    [string]$TaskName = "LEaps Operator UI Tailscale Serve",
    [string]$StatusPath = "data/runtime/startup/leaps_operator_ui_tailscale_serve_status.json"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$env:PYTHONPATH = "src"

function Test-FlagEnabled {
    param([string]$Value)
    return $Value -notmatch '^(false|0|no)$'
}

function Resolve-RepoPath {
    param([string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return (Join-Path $Root $PathValue)
}

function Normalize-Items {
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

function Test-LocalOperatorUi {
    param([int]$PortValue)
    try {
        $response = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$PortValue/" -TimeoutSec 5
        return $response.StatusCode -eq 200 -and $response.Content -like "*LEaps Operator UI*"
    } catch {
        return $false
    }
}

function Find-OperatorUiProcess {
    param([int]$PortValue)
    $processes = Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match 'powershell|python|py' }
    foreach ($process in $processes) {
        $command = [string]$process.CommandLine
        if (-not $command) {
            continue
        }
        if ($command -like "*leaps_quant_engine.cli*" -and $command -like "*operator-ui*" -and $command -like "*$PortValue*") {
            return $process
        }
    }
    return $null
}

function Start-OperatorUi {
    param(
        [string]$ConfigValue,
        [string[]]$SleeveIdValues,
        [string]$HostValue,
        [int]$PortValue
    )
    $args = @(
        "-3", "-m", "leaps_quant_engine.cli", "operator-ui", $ConfigValue,
        "--host", $HostValue,
        "--port", [string]$PortValue
    )
    foreach ($sleeveId in $SleeveIdValues) {
        $args += @("--sleeve-id", $sleeveId)
    }
    return Start-Process -FilePath "py" -ArgumentList $args -WorkingDirectory $Root -WindowStyle Hidden -PassThru
}

function Ensure-TailscaleServe {
    param(
        [int]$PortValue,
        [int]$HttpsPortValue
    )
    $tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $tailscale) {
        return [ordered]@{
            ok = $false
            error = "tailscale_cli_not_found"
            url = $null
            ip_url = $null
        }
    }

    $statusRaw = & tailscale status --json 2>&1
    if ($LASTEXITCODE -ne 0) {
        return [ordered]@{
            ok = $false
            error = "tailscale_status_failed:$statusRaw"
            url = $null
            ip_url = $null
        }
    }
    $status = $statusRaw | ConvertFrom-Json
    if ([string]$status.BackendState -ne "Running") {
        return [ordered]@{
            ok = $false
            error = "tailscale_not_running:$($status.BackendState)"
            url = $null
            ip_url = $null
        }
    }

    $serveOutput = & tailscale serve --bg "--http=$PortValue" --yes $PortValue 2>&1
    $serveExit = $LASTEXITCODE
    $httpsOutput = & tailscale serve --bg "--https=$HttpsPortValue" --yes $PortValue 2>&1
    $httpsExit = $LASTEXITCODE
    $dnsName = ([string]$status.Self.DNSName).TrimEnd(".")
    $ip = @($status.TailscaleIPs)[0]
    return [ordered]@{
        ok = ($serveExit -eq 0 -and $httpsExit -eq 0)
        error = if ($serveExit -eq 0 -and $httpsExit -eq 0) { $null } else { "tailscale_serve_failed:http=$serveOutput https=$httpsOutput" }
        url = if ($dnsName) { "https://$dnsName`:$HttpsPortValue/" } else { $null }
        http_url = if ($dnsName) { "http://$dnsName`:$PortValue/" } else { $null }
        ip_url = if ($ip) { "http://$ip`:$PortValue/" } else { $null }
        serve_output = [string]$serveOutput
        https_output = [string]$httpsOutput
    }
}

function Ensure-ScheduledTask {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [string]$ConfigValue,
        [string[]]$SleeveIdValues,
        [string]$HostValue,
        [int]$PortValue,
        [int]$HttpsPortValue
    )
    try {
        $sleeveArg = ($SleeveIdValues -join ",")
        $arguments = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$ScriptPath`"",
            "-Config", "`"$ConfigValue`"",
            "-SleeveIds", "`"$sleeveArg`"",
            "-HostName", "`"$HostValue`"",
            "-Port", [string]$PortValue,
            "-HttpsPort", [string]$HttpsPortValue,
            "-InstallScheduledTask", "false"
        ) -join " "
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Description "Start LEaps Operator UI on a fixed local port and publish it through Tailscale Serve." -Force -ErrorAction Stop | Out-Null
        return [ordered]@{
            ok = $true
            method = "scheduled_task"
            task_name = $Name
            error = $null
        }
    } catch {
        $taskError = $_.Exception.Message
        $shortcutResult = Ensure-StartupShortcut `
            -Name $Name `
            -ScriptPath $ScriptPath `
            -ConfigValue $ConfigValue `
            -SleeveIdValues $SleeveIdValues `
            -HostValue $HostValue `
            -PortValue $PortValue `
            -HttpsPortValue $HttpsPortValue
        return [ordered]@{
            ok = [bool]$shortcutResult.ok
            method = if ($shortcutResult.ok) { "startup_shortcut" } else { "failed" }
            task_name = $Name
            scheduled_task_error = $taskError
            startup_shortcut = $shortcutResult
            error = if ($shortcutResult.ok) { $null } else { $taskError }
        }
    }
}

function Ensure-StartupShortcut {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [string]$ConfigValue,
        [string[]]$SleeveIdValues,
        [string]$HostValue,
        [int]$PortValue,
        [int]$HttpsPortValue
    )
    try {
        $startup = [Environment]::GetFolderPath("Startup")
        if (-not $startup) {
            throw "startup_folder_not_found"
        }
        $sleeveArg = ($SleeveIdValues -join ",")
        $arguments = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$ScriptPath`"",
            "-Config", "`"$ConfigValue`"",
            "-SleeveIds", "`"$sleeveArg`"",
            "-HostName", "`"$HostValue`"",
            "-Port", [string]$PortValue,
            "-HttpsPort", [string]$HttpsPortValue,
            "-InstallScheduledTask", "false"
        ) -join " "
        $shortcutPath = Join-Path $startup "$Name.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = "powershell.exe"
        $shortcut.Arguments = $arguments
        $shortcut.WorkingDirectory = $Root
        $shortcut.WindowStyle = 7
        $shortcut.Description = "Start LEaps Operator UI on a fixed local port and publish it through Tailscale Serve."
        $shortcut.Save()
        return [ordered]@{
            ok = $true
            path = $shortcutPath
            error = $null
        }
    } catch {
        return [ordered]@{
            ok = $false
            path = $null
            error = $_.Exception.Message
        }
    }
}

$sleeves = @(Normalize-Items -Items $SleeveIds)
$statusFullPath = Resolve-RepoPath $StatusPath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $statusFullPath) | Out-Null

$operatorProcess = Find-OperatorUiProcess -PortValue $Port
$localReady = Test-LocalOperatorUi -PortValue $Port
$startedProcess = $null
if (-not $localReady) {
    $startedProcess = Start-OperatorUi -ConfigValue $Config -SleeveIdValues $sleeves -HostValue $HostName -PortValue $Port
    Start-Sleep -Seconds 4
    $localReady = Test-LocalOperatorUi -PortValue $Port
    $operatorProcess = Find-OperatorUiProcess -PortValue $Port
}

$tailscaleServe = Ensure-TailscaleServe -PortValue $Port -HttpsPortValue $HttpsPort
$taskResult = $null
if (Test-FlagEnabled $InstallScheduledTask) {
    $taskResult = Ensure-ScheduledTask `
        -Name $TaskName `
        -ScriptPath (Join-Path $Root "tools/leaps_operator_ui_tailscale_serve.ps1") `
        -ConfigValue $Config `
        -SleeveIdValues $sleeves `
        -HostValue $HostName `
        -PortValue $Port `
        -HttpsPortValue $HttpsPort
}

$payload = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    config = $Config
    sleeve_ids = $sleeves
    local = [ordered]@{
        host = $HostName
        port = $Port
        https_port = $HttpsPort
        ready = $localReady
        url = "http://127.0.0.1:$Port/"
        pid = if ($operatorProcess) { $operatorProcess.ProcessId } elseif ($startedProcess) { $startedProcess.Id } else { $null }
        action = if ($startedProcess) { "started" } elseif ($localReady) { "already_running" } else { "unavailable" }
    }
    tailscale = $tailscaleServe
    scheduled_task = $taskResult
}

$json = $payload | ConvertTo-Json -Depth 8
$json | Set-Content -Path $statusFullPath -Encoding UTF8
Write-Output $json
