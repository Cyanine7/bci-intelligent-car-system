[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AppArguments)

$ErrorActionPreference = "Stop"
$projectPath = $PSScriptRoot
$venvPython = Join-Path $projectPath ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Project environment is missing. First run:" -ForegroundColor Red
    Write-Host ('powershell -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $projectPath "setup.ps1") + '"')
    exit 1
}

$runExitCode = 1
try {
    Push-Location -LiteralPath $projectPath
    try {
        & $venvPython -m car_host @AppArguments
        $runExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} catch {
    Write-Host ("Startup failed: " + $_.Exception.Message) -ForegroundColor Red
}
exit $runExitCode
