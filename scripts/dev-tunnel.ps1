# Expose local Flask backend (5000) via Microsoft Dev Tunnels.
# Flask serves HTTP locally — MUST use --protocol http.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

Write-Host ""
Write-Host "Prerequisite: python app.py running on port 5000"
Write-Host "Starting public Dev Tunnel for port 5000..."
Write-Host "Use the printed https://*-5000.inc1.devtunnels.ms URL for OAuth/webhooks."
Write-Host ""

devtunnel host -p 5000 --allow-anonymous --protocol http
