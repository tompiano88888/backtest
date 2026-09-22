param(
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$DataRoot = "data"
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & "$projectRoot\.venv\Scripts\python.exe" -m backtest update $Config --root $DataRoot
    if ($LASTEXITCODE -ne 0) { throw "Data update failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}
