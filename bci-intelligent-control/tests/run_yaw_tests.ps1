$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    $vc = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat'
    $compile = '"' + $vc + '" >nul && cl.exe /nologo /utf-8 /W4 tests\test_yaw_algorithm.c BALANCE\yaw_control.c /Fe:tests\test_yaw_algorithm.exe /link /INCREMENTAL:NO'
    & cmd.exe /d /s /c $compile
    if ($LASTEXITCODE -ne 0) { throw 'Yaw algorithm compile failed' }
    & '.\tests\test_yaw_algorithm.exe'
    if ($LASTEXITCODE -ne 0) { throw 'Yaw algorithm failed' }
    $compile = '"' + $vc + '" >nul && cl.exe /nologo /utf-8 /W4 /LD tests\project_core_fixture.c BALANCE\speed_control.c BALANCE\speed_capture.c BALANCE\speed_experiment.c BALANCE\yaw_control.c BALANCE\yaw_experiment.c /Fe:tests\yaw_core_fixture.dll /link /INCREMENTAL:NO'
    & cmd.exe /d /s /c $compile
    if ($LASTEXITCODE -ne 0) { throw 'Yaw bridge compile failed' }
    & '..\上位机\.venv\Scripts\python.exe' -X utf8 'tests\test_yaw_bridge.py' 'tests\yaw_core_fixture.dll'
    if ($LASTEXITCODE -ne 0) { throw 'Actual Host/C yaw bridge failed' }
} finally { Pop-Location }
