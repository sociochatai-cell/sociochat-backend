# Run the WhatsApp queue WORKER locally (Option B). Consumes the Redis job queue
# (drip/bulk/warmup/etc.) enqueued by the API. Forces the LOCAL Docker services.
# Usage:  .\run-local-worker.ps1   (in a SECOND terminal, after the API is up)
$env:SQLALCHEMY_DATABASE_URI = "postgresql://sociochat:sociochat@localhost:5432/sociochat"
$env:REDIS_URL               = "redis://localhost:6379/0"
$env:JOB_QUEUE_BACKEND       = "redis"
$env:QDRANT_ENDPOINT         = "http://localhost:6333"
$env:FLASK_ENV               = "development"
Write-Host "Starting WhatsApp queue worker  (Redis = local Docker)" -ForegroundColor Green
& "$PSScriptRoot\myenv\Scripts\python.exe" "$PSScriptRoot\worker.py"
