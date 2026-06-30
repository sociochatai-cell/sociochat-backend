# ============================================================================
#  Connect the merged app to the GCP prod DB, SAFELY.
#  Step 1: backs up prod (aborts if the backup fails).
#  Step 2: asks you to type MIGRATE to confirm.
#  Step 3: boots the merged app against prod — which UPGRADES the prod schema
#          (adds columns/tables/FK changes) on first boot. Data is untouched.
#
#  Run from the backend folder. You need:
#    - PostgreSQL client tools (pg_dump) on PATH
#    - your machine's IP in Cloud SQL -> Connections -> Authorized networks
#      (or run the Cloud SQL Auth Proxy and use 127.0.0.1)
#
#  Usage:
#    .\migrate-to-prod.ps1 -ProdUrl "postgresql://dbuser:REALPASS@34.10.193.3:5432/sociochat"
#  (Get the REAL connection string from env.yaml, not the .env placeholder.)
# ============================================================================
param(
  [Parameter(Mandatory = $true)]
  [string]$ProdUrl
)
$ErrorActionPreference = "Stop"

$ts     = Get-Date -Format "yyyy-MM-dd_HHmmss"
$backup = "sociochat_prod_backup_$ts.dump"

Write-Host "STEP 1/3  Backing up prod -> $backup" -ForegroundColor Cyan
pg_dump $ProdUrl -Fc -f $backup
if (-not (Test-Path $backup) -or (Get-Item $backup).Length -eq 0) {
  Write-Host "Backup failed/empty. ABORTING — prod was NOT touched." -ForegroundColor Red
  exit 1
}
$mb = [math]::Round((Get-Item $backup).Length / 1MB, 1)
Write-Host "Backup OK ($mb MB). Keep $backup safe." -ForegroundColor Green

Write-Host ""
Write-Host "STEP 2/3  Next boot will UPGRADE the LIVE prod schema (irreversible without the backup)." -ForegroundColor Yellow
$ans = Read-Host "Type MIGRATE to proceed (anything else aborts)"
if ($ans -ne "MIGRATE") {
  Write-Host "Aborted. Prod NOT touched. Backup kept at $backup." -ForegroundColor Yellow
  exit 0
}

Write-Host "STEP 3/3  Booting merged app against prod (migrates on startup; watch for errors)..." -ForegroundColor Cyan
$env:SQLALCHEMY_DATABASE_URI = $ProdUrl
$env:FLASK_ENV               = "production"
& "$PSScriptRoot\myenv\Scripts\python.exe" "$PSScriptRoot\app.py"
