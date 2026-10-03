param(
    [string]$Python = '..\上位机\.venv\Scripts\python.exe',
    [string]$VcVars = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat'
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    if (!(Test-Path -LiteralPath $VcVars)) { throw 'MSVC x64 toolchain was not found' }
    $compileCommand = '"' + $VcVars + '" >nul && cl.exe /nologo /W4 /LD tests\project_core_fixture.c /Fe:tests\project_core_fixture.dll /Fo:tests\project_core_fixture.obj /link /INCREMENTAL:NO'
    & cmd.exe /d /s /c $compileCommand
    if ($LASTEXITCODE -ne 0) { throw 'C core test compilation failed' }
    & $Python 'tests\test_project_core.py' 'tests\project_core_fixture.dll'
    if ($LASTEXITCODE -ne 0) { throw 'C core protocol/control test failed' }
    & $Python 'tests\test_balance_runtime.py'
    if ($LASTEXITCODE -ne 0) { throw 'Balance runtime test failed' }
    & $Python 'tests\test_host_mcu_bridge.py'
    if ($LASTEXITCODE -ne 0) { throw 'Host/MCU software bridge test failed' }
} finally {
    Pop-Location
}
