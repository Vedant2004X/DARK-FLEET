$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot 'backend\.venv\Scripts\python.exe'
$Worker = Join-Path $ProjectRoot 'backend\worker\grid_ingestion_worker.py'
if (-not (Test-Path $Python)) { throw "Python environment not found: $Python" }
if (-not $env:GFW_API_ACCESS_TOKEN) { throw 'GFW_API_ACCESS_TOKEN is not configured in this PowerShell session.' }
& $Python $Worker --mode recent --max-reports 20 --safe-lag-days 7 --overlap-days 7 --max-new-days 7
exit $LASTEXITCODE
