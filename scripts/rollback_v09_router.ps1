$ErrorActionPreference = 'Stop'
$mainPath = Join-Path (Get-Location) 'backend\app\main.py'
if (-not (Test-Path $mainPath)) { throw 'Run this script from the DARK-FLEET project root.' }
$backup = Get-ChildItem -LiteralPath (Split-Path $mainPath) -Filter 'main.py.v085_backup_*' -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $backup) { throw 'No v0.8.5 backup was found. Nothing was changed.' }
Copy-Item -LiteralPath $backup.FullName -Destination $mainPath -Force
Write-Host "Restored: $($backup.FullName)" -ForegroundColor Green
