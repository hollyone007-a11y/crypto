# Registers the paper agent as a Windows scheduled task for the CURRENT user.
# Starts at logon, restarts every minute on failure, no time limit.
# Usage (PowerShell, from the project folder):   .\ops\install_task.ps1
# Remove:                                        Unregister-ScheduledTask -TaskName futures-agent -Confirm:$false
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "venv not found: $pyw" }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "-m ops.background" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "futures-agent" -Action $action -Trigger $trigger -Settings $settings `
    -Description "futures-agent PAPER trading (ops.run)" -Force | Out-Null
Start-ScheduledTask -TaskName "futures-agent"
Write-Host "Registered and started. Dashboard: http://127.0.0.1:8765  Logs: $root\logs\agent.log"
