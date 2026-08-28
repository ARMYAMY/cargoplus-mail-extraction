param(
    [ValidateRange(1, 65535)]
    [int]$ServicePort = 8001
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python virtual environment does not exist: $pythonPath"
}

$baseUrl = "https://api.senseaudio.cn/v1"
if (Test-Path -LiteralPath $envPath -PathType Leaf) {
    foreach ($line in [IO.File]::ReadAllLines($envPath)) {
        if ($line -match '^\s*LLM_BASE_URL\s*=\s*(.+?)\s*$') {
            $baseUrl = $matches[1].Trim().Trim('"').Trim("'")
            break
        }
    }
}

try {
    $llmUri = [Uri]$baseUrl
} catch {
    throw "LLM_BASE_URL is invalid: $baseUrl"
}
$llmPort = if ($llmUri.Port -gt 0) { $llmUri.Port } elseif ($llmUri.Scheme -eq "https") { 443 } else { 80 }
$networkReady = Test-NetConnection -ComputerName $llmUri.Host -Port $llmPort -InformationLevel Quiet
if (-not $networkReady) {
    throw (
        "Cannot reach $($llmUri.Host):$llmPort. The existing 8001 service was not stopped. " +
        "Run this script from a normal PowerShell session with outbound network access."
    )
}

$listenerLine = netstat -ano |
    Select-String ":$ServicePort\s+.*LISTENING" |
    Select-Object -First 1
if ($listenerLine) {
    $listenerProcessId = [int](($listenerLine.ToString() -split '\s+')[-1])
    $listenerProcess = Get-Process -Id $listenerProcessId
    if ($listenerProcess.ProcessName -notmatch '^python(?:w)?$') {
        throw "Port $ServicePort is occupied by a non-Python process (PID $listenerProcessId)."
    }
    Stop-Process -Id $listenerProcessId -Force
}

$startOptions = @{
    FilePath = $pythonPath
    ArgumentList = @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', "$ServicePort")
    WorkingDirectory = $projectRoot
    WindowStyle = 'Hidden'
    PassThru = $true
}
$serviceProcess = Start-Process @startOptions

$healthUrl = "http://127.0.0.1:$ServicePort/health"
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.status -eq "healthy") {
            Write-Host "CargoPlus 8001 started successfully. PID=$($serviceProcess.Id)"
            exit 0
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

throw "CargoPlus did not become healthy at $healthUrl within 15 seconds."
