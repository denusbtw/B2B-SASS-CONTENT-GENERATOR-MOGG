param([int] $Port = 18081, [switch] $Recovery)
$ErrorActionPreference = 'Stop'
if (-not $env:DATABASE_URL) { throw 'Set DATABASE_URL first' }
$env:OPERATOR_TOKEN = [guid]::NewGuid().ToString('N')
$env:WORKER_AUTH_TOKEN = [guid]::NewGuid().ToString('N')
$env:WORKER_API_URL = "http://127.0.0.1:$Port"
$env:PYTHONUTF8 = '1'
$python = (Get-Item -LiteralPath '.venv\Scripts\python.exe').FullName
New-Item -ItemType Directory -Force -Path 'data' | Out-Null
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Migration failed' }
$api = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','main:app','--host','127.0.0.1','--port',$Port) -WindowStyle Hidden -RedirectStandardError 'data/api-error.log' -RedirectStandardOutput 'data/api.log' -PassThru
try {
    $ready = $false
    for ($i = 0; $i -lt 25; $i++) {
        if ($api.HasExited) { Get-Content -LiteralPath 'data/api-error.log'; throw 'Coordinator exited' }
        try {
            Invoke-RestMethod -Uri "$env:WORKER_API_URL/api/v1/health/ready" -TimeoutSec 1 | Out-Null
            $ready = $true
            break
        } catch { Start-Sleep -Milliseconds 400 }
    }
    if (-not $ready) { throw 'Coordinator did not become ready' }
    & $python -m scripts.demo
    if ($LASTEXITCODE -ne 0) { throw 'Demo failed' }
    if ($Recovery) {
        & $python -m scripts.recover
        if ($LASTEXITCODE -ne 0) { throw 'Recovery demo failed' }
    }
} finally {
    if (-not $api.HasExited) { Stop-Process -Id $api.Id }
}
