param(
    [ValidateSet('Install','Start','Stop','Status','Uninstall')][string]$Action = 'Status',
    [string]$ConfigPath = ''
)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath 'venv\Scripts\pythonw.exe'
$workerPath = Join-Path $PSScriptRoot 'sync_worker.py'
$startupPath = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupPath 'Mizitco Inventory Sync.lnk'
switch ($Action) {
    'Install' {
        if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Project Python environment is missing.' }
        if (-not $ConfigPath) { throw 'Supply the sync-service.env path.' }
        $resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $pythonPath
        $shortcut.Arguments = '"' + $workerPath + '" --config "' + $resolvedConfig + '"'
        $shortcut.WorkingDirectory = $projectPath
        $shortcut.Description = 'Mizitco Inventory Access and website sync worker'
        $shortcut.WindowStyle = 7
        $shortcut.Save()
        Write-Output "Installed user sign-in startup shortcut: $shortcutPath"
    }
    'Start' {
        if (-not $ConfigPath) { throw 'Supply the sync-service.env path.' }
        $resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
        Start-Process -FilePath $pythonPath -ArgumentList ('"' + $workerPath + '" --config "' + $resolvedConfig + '"') -WorkingDirectory $projectPath -WindowStyle Hidden
        Write-Output 'Started sync worker in the current Windows session.'
    }
    'Stop' {
        $expected = $workerPath.ToLowerInvariant()
        $running = @(Get-CimInstance Win32_Process -Filter "name='pythonw.exe'" | Where-Object {
            $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($expected)
        })
        foreach ($process in $running) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Output ('Stopped ' + $running.Count + ' matching worker process entries.')
    }
    'Status' {
        Write-Output ('Startup shortcut installed: ' + (Test-Path -LiteralPath $shortcutPath))
        if (Test-Path -LiteralPath $shortcutPath) { Write-Output $shortcutPath }
    }
    'Uninstall' {
        if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
        Write-Output 'Removed user sign-in startup shortcut.'
    }
}
