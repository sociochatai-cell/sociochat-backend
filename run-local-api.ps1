# Run the SocioChat backend API locally against the LOCAL Docker dev DB.
# Forces local Postgres/Redis/Qdrant so it can NEVER touch the prod DB in .env.
# Usage:  .\run-local-api.ps1     (from the backend folder, or anywhere)
$env:SQLALCHEMY_DATABASE_URI = "postgresql://sociochat:sociochat@localhost:5432/sociochat"
$env:REDIS_URL               = "redis://localhost:6379/0"
$env:JOB_QUEUE_BACKEND       = "inline"   # API processes webhooks itself (no separate worker needed)
$env:QDRANT_ENDPOINT         = "http://localhost:6333"
$env:FLASK_ENV               = "development"
Write-Host "Starting API on http://localhost:5000  (DB = local Docker)" -ForegroundColor Green
& "$PSScriptRoot\myenv\Scripts\python.exe" "$PSScriptRoot\app.py"
