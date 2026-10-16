param(
    [Parameter(Mandatory=$true)][string]$SDKRoot
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create the project .venv first' }
$bits = & $pythonPath -c 'import struct; print(struct.calcsize("P") * 8)'
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine Python architecture' }
$architecture = if ($bits -eq '64') { 'x64' } else { 'x86' }
$binPath = Join-Path (Resolve-Path -LiteralPath $SDKRoot).Path 'bin'
$installer = Join-Path $binPath "$architecture\installiFinDPy.py"
if (-not (Test-Path -LiteralPath $installer)) { throw 'Official SDK installer was not found' }
& $pythonPath $installer $binPath
if ($LASTEXITCODE -ne 0) { throw 'iFinD SDK setup failed' }
Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -m backtest ifind-check
    if ($LASTEXITCODE -ne 0) { throw 'iFinD SDK import check failed' }
} finally {
    Pop-Location
}
