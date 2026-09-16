[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSCommandPath
$sourcePath = Join-Path $projectDir "codex_usage_widget.py"
$supervisorSourcePath = Join-Path $projectDir "codex_usage_supervisor.py"
$distDir = Join-Path $projectDir "dist"
$buildDir = Join-Path $projectDir "build\pyinstaller"
$specDir = Join-Path $projectDir "build"
$exePath = Join-Path $distDir "CodexUsageWidget.exe"
$supervisorExePath = Join-Path $distDir "CodexUsageSupervisor.exe"

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Source file not found: $sourcePath"
}
if (-not (Test-Path -LiteralPath $supervisorSourcePath -PathType Leaf)) {
    throw "Supervisor source file not found: $supervisorSourcePath"
}

$runningWidget = @(Get-Process -Name "CodexUsageWidget" -ErrorAction SilentlyContinue)
if ($runningWidget.Count -gt 0) {
    throw "Close CodexUsageWidget before rebuilding the EXE."
}

$pythonPath = $null
$projectVenvPython = Join-Path $projectDir ".venv311\Scripts\python.exe"
if (Test-Path -LiteralPath $projectVenvPython -PathType Leaf) {
    $pythonPath = $projectVenvPython
}
$pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($null -eq $pythonPath -and $null -ne $pyLauncher) {
    $candidate = @(& $pyLauncher.Source -3.11 -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $candidate.Count -gt 0) {
        $pythonPath = $candidate[-1]
    }
}
if ($null -eq $pythonPath) {
    $pythonCommand = Get-Command "python.exe" -ErrorAction Stop
    $pythonPath = $pythonCommand.Source
}

Push-Location $projectDir
try {
    & $pythonPath -m PyInstaller `
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

    & $pythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name CodexUsageSupervisor `
        --distpath $distDir `
        --workpath (Join-Path $buildDir "supervisor") `
        --specpath $specDir `
        --log-level WARN `
        $supervisorSourcePath
    if ($LASTEXITCODE -ne 0) {
        throw "Supervisor PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "EXE was not created: $exePath"
}
if (-not (Test-Path -LiteralPath $supervisorExePath -PathType Leaf)) {
    throw "Supervisor EXE was not created: $supervisorExePath"
}

Write-Output $exePath
Write-Output $supervisorExePath
