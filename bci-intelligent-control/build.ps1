param(
    [string]$Keil = 'D:\Application\Keil5 MDK\UV4\UV4.exe',
    [string]$Python = '..\上位机\.venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (!(Test-Path -LiteralPath $Keil)) { throw "Keil executable not found: $Keil" }
    $buildStarted = Get-Date
    $compiler = Start-Process -FilePath $Keil -ArgumentList @('-r','USER\WHEELTEC.uvprojx','-t','FreeRTOS','-j0','-o','keil_build.log') -WindowStyle Hidden -PassThru -Wait
    # uVision's launcher exit code alone does not confirm successful compilation.
    $buildLog = Get-Item -LiteralPath 'USER\keil_build.log'
    if ($buildLog.LastWriteTime -lt $buildStarted) { throw 'Build log was not refreshed' }
    $resultText = Get-Content -LiteralPath $buildLog.FullName -Raw
    if (!$resultText.Contains('0 Error(s), 0 Warning(s)')) { throw $resultText }
    $firmwareFile = Get-Item -LiteralPath 'WHEELTEC.bin'
    if ($firmwareFile.LastWriteTime -lt $buildStarted) { throw 'BIN was not regenerated' }
    & $Python 'tools\verify_firmware.py'
    if ($LASTEXITCODE -ne 0) { throw 'Firmware verification failed' }
    Write-Output "Ready: $($firmwareFile.FullName)"
} finally {
    Pop-Location
}
