param(
    [string]$Python = '..\上位机\.venv\Scripts\python.exe',
    [string]$VcVars = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat'
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    if (!(Test-Path -LiteralPath $VcVars)) { throw 'MSVC x64 toolchain was not found' }
    $algorithmCompile = '"' + $VcVars + '" >nul && cl.exe /nologo /utf-8 /W4 tests\test_speed_algorithm.c BALANCE\speed_control.c /Fe:tests\test_speed_algorithm.exe /link /INCREMENTAL:NO'
    & cmd.exe /d /s /c $algorithmCompile
    if ($LASTEXITCODE -ne 0) { throw 'Production PI test compilation failed' }
    & '.\tests\test_speed_algorithm.exe'
    if ($LASTEXITCODE -ne 0) { throw 'Production PI behavior test failed' }
    $compileCommand = '"' + $VcVars + '" >nul && cl.exe /nologo /utf-8 /W4 /LD tests\project_core_fixture.c BALANCE\speed_control.c BALANCE\speed_capture.c BALANCE\speed_experiment.c BALANCE\yaw_control.c BALANCE\yaw_experiment.c /Fe:tests\project_core_fixture.dll /link /INCREMENTAL:NO'
    & cmd.exe /d /s /c $compileCommand
    if ($LASTEXITCODE -ne 0) { throw 'C core test compilation failed' }
    & $Python 'tests\test_project_core.py' 'tests\project_core_fixture.dll'
    if ($LASTEXITCODE -ne 0) { throw 'C core protocol/control test failed' }
    & $Python 'tests\test_speed_core.py' 'tests\project_core_fixture.dll' '1'
    if ($LASTEXITCODE -ne 0) { throw 'V2 real C protocol/capture test failed' }
    $speedCompile = '"' + $VcVars + '" >nul && cl.exe /nologo /utf-8 /W4 /DSPEED_CONTROL_IMPLEMENTED=1 /LD tests\project_core_fixture.c tests\speed_algorithm_fixture.c BALANCE\speed_capture.c BALANCE\speed_experiment.c BALANCE\yaw_control.c BALANCE\yaw_experiment.c /Fe:tests\speed_core_fixture.dll /link /INCREMENTAL:NO'
    & cmd.exe /d /s /c $speedCompile
    if ($LASTEXITCODE -ne 0) { throw 'Speed protocol test-double compilation failed' }
    & $Python 'tests\test_speed_core.py' 'tests\speed_core_fixture.dll' '1'
    if ($LASTEXITCODE -ne 0) { throw 'Speed protocol test-double regression failed' }
    & $Python 'tests\test_speed_bridge.py' 'tests\project_core_fixture.dll' '1'
    if ($LASTEXITCODE -ne 0) { throw 'V2 actual Host/production core bridge failed' }
    & $Python 'tests\test_speed_bridge.py' 'tests\speed_core_fixture.dll' '1'
    if ($LASTEXITCODE -ne 0) { throw 'V2 binary64 Host/test-double core bridge failed' }
    & $Python 'tests\test_balance_runtime.py'
    if ($LASTEXITCODE -ne 0) { throw 'Balance runtime test failed' }
    & $Python 'tests\test_imu_runtime.py'
    if ($LASTEXITCODE -ne 0) { throw 'IMU runtime test failed' }
    & $Python 'tests\test_host_mcu_bridge.py'
    if ($LASTEXITCODE -ne 0) { throw 'Host/MCU software bridge test failed' }
} finally {
    Pop-Location
}
