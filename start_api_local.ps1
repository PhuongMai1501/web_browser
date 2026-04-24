# start_api_local.ps1 — Start API trên laptop Windows (PowerShell).
#
# Prerequisites:
#   - Docker Desktop đã chạy
#   - Redis container running: docker start redis-phase1  (hoặc tạo mới)
#   - pip install -r requirements.txt
#
# Usage:
#   cd dev/deploy_server
#   .\start_api_local.ps1

$ErrorActionPreference = "Stop"
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR

# Load .env
if (Test-Path ".env") {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
    Write-Host "[API] Loaded .env" -ForegroundColor Green
} else {
    Write-Error "[API] .env not found"
    exit 1
}

# Verify Redis reachable
try {
    $redisUrl = $env:REDIS_URL
    if (-not $redisUrl) { $redisUrl = "redis://localhost:6379/0" }
    $redisHost = ($redisUrl -replace "redis://", "" -split "[:/]")[0]
    $redisPort = ($redisUrl -replace "redis://", "" -split "[:/]")[1]
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect($redisHost, $redisPort)
    $tcp.Close()
    Write-Host "[API] Redis OK at ${redisHost}:${redisPort}" -ForegroundColor Green
} catch {
    Write-Warning "[API] Redis not reachable. Run: docker start redis-phase1"
}

# Set PYTHONPATH + start uvicorn
$env:PYTHONPATH = "$SCRIPT_DIR\ai_tool_web;$env:PYTHONPATH"
Set-Location ai_tool_web

Write-Host "[API] Starting uvicorn on http://127.0.0.1:9000 ..." -ForegroundColor Cyan
python -m uvicorn api.app:app --host 127.0.0.1 --port 9000 --reload
