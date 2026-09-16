[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSCommandPath
$exePath = Join-Path $projectDir "CodexUsageWidget.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    $exePath = Join-Path $projectDir "dist\CodexUsageWidget.exe"
}
$supervisorPath = Join-Path $projectDir "CodexUsageSupervisor.exe"
if (-not (Test-Path -LiteralPath $supervisorPath -PathType Leaf)) {
    $supervisorPath = Join-Path $projectDir "dist\CodexUsageSupervisor.exe"
}
$widgetSourcePath = Join-Path $projectDir "codex_usage_widget.py"
$supervisorSourcePath = Join-Path $projectDir "codex_usage_supervisor.py"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "Codex Usage Widget.lnk"
$taskName = "Codex Usage Widget Supervisor"

if ($Remove) {
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
    Write-Output "Removed task: $taskName"
    Write-Output "Removed: $shortcutPath"
    exit 0
}

$pythonwPath = $null
$projectVenvPythonw = Join-Path $projectDir ".venv311\Scripts\pythonw.exe"
if (Test-Path -LiteralPath $projectVenvPythonw -PathType Leaf) {
    $pythonwPath = $projectVenvPythonw
}
$pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($null -eq $pythonwPath -and $null -ne $pyLauncher) {
    $candidate = @(& $pyLauncher.Source -3.11 -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $candidate.Count -gt 0) {
        $candidatePythonw = Join-Path (Split-Path -Parent $candidate[-1].Trim()) "pythonw.exe"
        if (Test-Path -LiteralPath $candidatePythonw -PathType Leaf) {
            $pythonwPath = $candidatePythonw
        }
    }
}

$sourceMode = (
    $null -ne $pythonwPath -and
    (Test-Path -LiteralPath $widgetSourcePath -PathType Leaf) -and
    (Test-Path -LiteralPath $supervisorSourcePath -PathType Leaf)
)
if ($sourceMode) {
    $launcherPath = $pythonwPath
    $launcherArguments = '"{0}"' -f $supervisorSourcePath
    $iconPath = if (Test-Path -LiteralPath $exePath -PathType Leaf) { $exePath } else { $pythonwPath }
}
else {
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Installed EXE not found: $exePath"
    }
    if (-not (Test-Path -LiteralPath $supervisorPath -PathType Leaf)) {
        throw "Windowless supervisor not found: $supervisorPath"
    }
    $launcherPath = $supervisorPath
    $launcherArguments = ""
    $iconPath = $exePath
}

$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $launcherPath
    $shortcut.Arguments = $launcherArguments
    $shortcut.WorkingDirectory = $projectDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Codex usage HUD supervisor"
    $shortcut.IconLocation = "$iconPath,0"
    $shortcut.Save()

    $saved = $shell.CreateShortcut($shortcutPath)
    $resolvedTarget = [System.IO.Path]::GetFullPath($saved.TargetPath)
    $expectedTarget = [System.IO.Path]::GetFullPath($launcherPath)
    if ($resolvedTarget -ne $expectedTarget) {
        throw "Shortcut target mismatch: $resolvedTarget"
    }
}
finally {
    if ($null -ne $saved) {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($saved)
    }
    if ($null -ne $shortcut) {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
    }
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction `
    -Execute $launcherPath `
    -Argument $launcherArguments `
    -WorkingDirectory $projectDir
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$heartbeatTrigger = New-ScheduledTaskTrigger -Daily -At "00:00"
$heartbeatRepetition = New-CimInstance `
    -ClassName MSFT_TaskRepetitionPattern `
    -Namespace "Root/Microsoft/Windows/TaskScheduler" `
    -ClientOnly `
    -Property @{
        Interval = "PT1M"
        Duration = "P1D"
        StopAtDurationEnd = $false
    }
$heartbeatTrigger.Repetition = $heartbeatRepetition
$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($logonTrigger, $heartbeatTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description "Keeps the Codex usage taskbar widget running for the current signed-in user." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Output "Installed: $shortcutPath"
Write-Output "Task:      $taskName"
Write-Output "Launcher:  $launcherPath $launcherArguments"
Write-Output "Mode:      $(if ($sourceMode) { 'signed pythonw source host' } else { 'packaged EXE' })"
