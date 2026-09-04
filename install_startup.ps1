[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSCommandPath
$exePath = Join-Path $projectDir "dist\CodexUsageWidget.exe"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "Codex Usage Widget.lnk"

if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
    Write-Output "Removed: $shortcutPath"
    exit 0
}

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Build the EXE first: $exePath"
}

$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $exePath
    $shortcut.WorkingDirectory = Split-Path -Parent $exePath
    $shortcut.Description = "Codex usage HUD"
    $shortcut.IconLocation = "$exePath,0"
    $shortcut.Save()

    $saved = $shell.CreateShortcut($shortcutPath)
    $resolvedTarget = [System.IO.Path]::GetFullPath($saved.TargetPath)
    $expectedTarget = [System.IO.Path]::GetFullPath($exePath)
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

Write-Output "Installed: $shortcutPath"
Write-Output "Target:    $exePath"
