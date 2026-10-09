param(
    [ValidateSet('Install','Start','Stop','Status','Uninstall')][string]$Action = 'Status',
    [string]$ConfigPath = '',
    [string]$ServiceAccount = 'NT AUTHORITY\LOCAL SERVICE'
)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath 'venv\Scripts\pythonw.exe'
$workerPath = Join-Path $PSScriptRoot 'sync_worker.py'
$taskName = 'Mizitco Inventory Sync'
switch ($Action) {
    'Install' {
        if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create the project virtual environment first.' }
        if (-not $ConfigPath) { throw 'Supply an absolute path to the protected sync-service.env file.' }
        $resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
        $arguments = '"' + $workerPath + '" --config "' + $resolvedConfig + '"'
        $taskAction = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectPath
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType ServiceAccount -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal
        Write-Output 'Installed. Grant the service account access to the protected config, log folder, and staged Access file before starting.'
    }
    'Start' { Start-ScheduledTask -TaskName $taskName }
    'Stop' { Stop-ScheduledTask -TaskName $taskName }
    'Status' { Get-ScheduledTask -TaskName $taskName; Get-ScheduledTaskInfo -TaskName $taskName }
    'Uninstall' { Unregister-ScheduledTask -TaskName $taskName -Confirm:$true }
}
