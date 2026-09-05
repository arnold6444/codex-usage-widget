[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSCommandPath
$taskName = "Codex Usage Widget"
$installDir = Join-Path $projectDir "dist"
$exePath = Join-Path $installDir "CodexUsageWidget.exe"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "Codex Usage Widget.lnk"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
    Write-Output "Automatic recovery disabled. The running widget can now be closed."
    exit 0
}

$sourceExe = Join-Path $projectDir "dist\CodexUsageWidget.exe"
if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
    throw "Build the EXE first: $sourceExe"
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $installDir
$logon = New-ScheduledTaskTrigger -AtLogOn -User $userId
$recovery = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon, $recovery) -Principal $principal -Settings $settings -Description "Keep the usage HUD running independently of Codex; recover within one minute." -Force | Out-Null
$saved = Get-ScheduledTask -TaskName $taskName
if ($saved.Actions.Execute -ne $exePath) {
    throw "Scheduled task executable verification failed."
}
if ($saved.State -ne 'Running') {
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 3
}
$runInfo = Get-ScheduledTaskInfo -TaskName $taskName
if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running' -and
    $runInfo.LastTaskResult -notin @(0, 267009)) {
    throw "Scheduled launch failed with code $($runInfo.LastTaskResult); the previous shortcut was retained."
}

# Retire the previous login-only launcher after the replacement is registered.
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
}
Write-Output "Installed scheduled task: $taskName"
Write-Output "Target: $exePath"
Write-Output "Recovery: login and every minute; independent of the Codex app."
