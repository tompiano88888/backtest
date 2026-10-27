param(
    [ValidateSet('check', 'demo', 'rbreaker', 'arbitrage')]
    [string]$Task = 'check'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Restore the project .venv first; see docs/WORKFLOW_RESTORE.md' }
function Invoke-ProjectPython {
    & $pythonPath @args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}
Push-Location -LiteralPath $projectRoot
$previousMplConfig = $env:MPLCONFIGDIR
try {
    # A unique directory prevents accidental replacement of checkpoints or previous runs.
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $runRoot = "outputs/workflow_${stamp}_$([guid]::NewGuid().ToString('N').Substring(0,8))"
    $env:MPLCONFIGDIR = Join-Path $projectRoot "$runRoot/matplotlib"
    New-Item -ItemType Directory -Path $env:MPLCONFIGDIR -Force | Out-Null
    switch ($Task) {
        'check' {
            Invoke-ProjectPython -m pytest -q -p no:cacheprovider
            Invoke-ProjectPython -m compileall -q backtest infinitrader_adapter platform_strategies scripts
            Invoke-ProjectPython -m backtest demo --output "$runRoot/demo.json"
        }
        'demo' {
            Invoke-ProjectPython -m backtest demo --output "$runRoot/demo.json"
            Invoke-ProjectPython -m backtest run examples/account_events.synthetic.json --output "$runRoot/account_events.json"
        }
        'rbreaker' {
            $resultRoot = "${runRoot}/rbreaker_historical_fees"
            Invoke-ProjectPython -m scripts.run_rbreaker_portfolio --stop-risk .01 --include-auction --historical-fees configs/ifind.index-futures-fees.json --avoid-close-today --output $resultRoot
            Invoke-ProjectPython -m scripts.verify_rbreaker_fee_avoidance --root $resultRoot
        }
        'arbitrage' {
            Invoke-ProjectPython -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --dividends --minimum-return-basis zero_convergence --entry-floors configs/arbitrage_dividend_may_aug_entry_floors.json --dividend-season-adjustment --dividend-season-months '5,6,7,8' --rolling-source outputs/arbitrage_dividend_may_aug_fixed_floors --output "$runRoot/arbitrage"
        }
    }
    Write-Host "Completed. New output directory: $runRoot"
} finally {
    $env:MPLCONFIGDIR = $previousMplConfig
    Pop-Location
}
