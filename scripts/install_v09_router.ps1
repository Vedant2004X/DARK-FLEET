$ErrorActionPreference = 'Stop'

$mainPath = Join-Path (Get-Location) 'backend\app\main.py'
$modulePath = Join-Path (Get-Location) 'backend\app\v09_analytics.py'
if (-not (Test-Path $mainPath)) { throw "Run this script from the DARK-FLEET project root. Could not find: $mainPath" }
if (-not (Test-Path $modulePath)) { throw "Copy backend\app\v09_analytics.py from this package before running the installer." }

$content = Get-Content -LiteralPath $mainPath -Raw
$marker = '# DARK-FLEET v0.9 analytics router'
if ($content.Contains($marker)) {
    Write-Host 'v0.9 router registration is already present. No changes made.' -ForegroundColor Yellow
    exit 0
}
if ($content -notmatch '(?m)^app\s*=\s*FastAPI\(') {
    throw 'Could not find the FastAPI app declaration in backend\app\main.py. No changes made.'
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backupPath = "$mainPath.v085_backup_$stamp"
Copy-Item -LiteralPath $mainPath -Destination $backupPath

$registration = @'

# DARK-FLEET v0.9 analytics router
from app.v09_analytics import router as v09_analytics_router
app.include_router(v09_analytics_router)
'@
Set-Content -LiteralPath $mainPath -Value ($content.TrimEnd() + $registration + "`r`n") -Encoding UTF8
Write-Host "v0.9 router registered." -ForegroundColor Green
Write-Host "Backup: $backupPath"
Write-Host 'The existing application routes and data files were preserved.'
