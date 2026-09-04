[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSCommandPath
$sourcePath = Join-Path $projectDir "codex_usage_widget.py"
$distDir = Join-Path $projectDir "dist"
$buildDir = Join-Path $projectDir "build\pyinstaller"
$specDir = Join-Path $projectDir "build"
$exePath = Join-Path $distDir "CodexUsageWidget.exe"

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Source file not found: $sourcePath"
}

$runningWidget = @(Get-Process -Name "CodexUsageWidget" -ErrorAction SilentlyContinue)
if ($runningWidget.Count -gt 0) {
    throw "Close CodexUsageWidget before rebuilding the EXE."
}

Push-Location $projectDir
try {
    & python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name CodexUsageWidget `
        --distpath $distDir `
        --workpath $buildDir `
        --specpath $specDir `
        --log-level WARN `
        $sourcePath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "EXE was not created: $exePath"
}

Write-Output $exePath
