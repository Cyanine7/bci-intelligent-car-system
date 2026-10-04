[CmdletBinding()]
param([string]$Python = "", [switch]$TestDependencies)

$ErrorActionPreference = "Stop"
$projectPath = $PSScriptRoot
$venvPath = Join-Path $projectPath ".venv-mcp"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $baseArguments = @()
        if ($Python) {
            $basePython = $Python
        } elseif (Test-Path -LiteralPath (Join-Path $projectPath ".venv\Scripts\python.exe")) {
            $basePython = Join-Path $projectPath ".venv\Scripts\python.exe"
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            $basePython = "py"
            $baseArguments = @("-3")
        } else {
            throw "需要 Python 3.11 至 3.14；可通过 -Python 指定绝对路径。"
        }
        & $basePython @baseArguments -c "import sys; assert (3, 11) <= sys.version_info < (3, 15)"
        if ($LASTEXITCODE -ne 0) { throw "Python 版本不受支持。" }
        & $basePython @baseArguments -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw "MCP 独立环境创建失败。" }
    }
    & $venvPython -m pip install -r (Join-Path $projectPath "requirements-mcp-lock.txt")
    if ($LASTEXITCODE -ne 0) { throw "官方 SDK 安装失败，请检查上面的 pip 输出。" }
    if ($TestDependencies) {
        & $venvPython -m pip install -r (Join-Path $projectPath "requirements-mcp-dev.txt")
        if ($LASTEXITCODE -ne 0) { throw "MCP 测试依赖安装失败。" }
    }
    Write-Host "MCP 环境已准备；从本目录运行 .venv-mcp\Scripts\python.exe -m car_debug_mcp。"
    exit 0
} catch {
    Write-Host ("MCP 安装失败：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
