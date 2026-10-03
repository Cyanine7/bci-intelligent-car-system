[CmdletBinding()]
param([string]$Python = "")

$ErrorActionPreference = "Stop"
$projectPath = $PSScriptRoot
$venvPath = Join-Path $projectPath ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $baseArguments = @()
        if ($Python) {
            $basePython = $Python
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            $basePython = "py"
            $baseArguments = @("-3")
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $basePython = "python"
        } else {
            throw "Python 3.11 to 3.14 is required. Run setup.ps1 -Python 'C:\path\to\python.exe'."
        }
        & $basePython @baseArguments -c "import sys; assert (3, 11) <= sys.version_info < (3, 15), 'Python 3.11 to 3.14 is required'"
        if ($LASTEXITCODE -ne 0) { throw "The selected Python interpreter could not be used." }
        & $basePython @baseArguments -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
    }
    & $venvPython -m pip install -r (Join-Path $projectPath "requirements-dev.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the pip output above." }
    Write-Host "Environment ready. Run start.ps1 to open the workbench."
    exit 0
} catch {
    Write-Host ("Setup failed: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
