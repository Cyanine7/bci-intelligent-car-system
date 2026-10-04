[CmdletBinding()]
param([switch]$Demo)

$ErrorActionPreference = "Stop"
$automationArguments = @("--automation", "--no-auto-connect")
if ($Demo) { $automationArguments += "--automation-demo" }
& (Join-Path $PSScriptRoot "start.ps1") @automationArguments
exit $LASTEXITCODE
