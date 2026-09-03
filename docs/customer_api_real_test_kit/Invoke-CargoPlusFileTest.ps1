[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$CaseId,

    [Parameter(Mandatory = $true)]
    [string[]]$FilePath,

    [ValidateSet('standard', 'high_accuracy')]
    [string]$RecognitionMode = 'standard',

    [ValidateRange(0, 99)]
    [int]$Attempt = 0,

    [string]$IdempotencyKey,

    [string]$MailSubject,

    [string]$CallbackUrl,

    [ValidateRange(1, 60)]
    [int]$PollIntervalSeconds = 3,

    [ValidateRange(1, 60)]
    [int]$TimeoutMinutes = 10,

    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'results'),

    [switch]$SkipHealthCheck
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

function Write-JsonFile {
    param([object]$Value, [string]$Path)
    $json = $Value | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($Path, $json, [System.Text.UTF8Encoding]::new($false))
}

function Read-JsonResponse {
    param([System.Net.Http.HttpResponseMessage]$Response)
    $body = $Response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    if ([string]::IsNullOrWhiteSpace($body)) {
        return [pscustomobject]@{ raw_body = ''; http_status = [int]$Response.StatusCode }
    }
    try {
        return $body | ConvertFrom-Json
    }
    catch {
        throw "Server returned non-JSON content (HTTP $([int]$Response.StatusCode)): $body"
    }
}

$apiKey = $env:CARGOPLUS_API_KEY
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw 'Environment variable CARGOPLUS_API_KEY is not set.'
}

$normalizedBaseUrl = $BaseUrl.TrimEnd('/')
$resolvedFiles = @()
foreach ($item in $FilePath) {
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) {
        throw "Input file not found: $item"
    }
    $resolvedFiles += (Resolve-Path -LiteralPath $item).Path
}
if ($resolvedFiles.Count -gt 10) {
    throw 'A task can contain at most 10 files.'
}

if ([string]::IsNullOrWhiteSpace($IdempotencyKey)) {
    $IdempotencyKey = "$CaseId-$RecognitionMode-r$Attempt"
}
if ($IdempotencyKey.Length -gt 128) {
    throw 'Idempotency-Key must not exceed 128 characters.'
}
if ([string]::IsNullOrWhiteSpace($MailSubject)) {
    $MailSubject = "$CaseId API file extraction test"
}

$runDirectory = Join-Path (Join-Path (Join-Path $OutputDirectory $CaseId) $RecognitionMode) "r$Attempt"
[System.IO.Directory]::CreateDirectory($runDirectory) | Out-Null

$requestMetadata = [ordered]@{
    case_id = $CaseId
    base_url = $normalizedBaseUrl
    recognition_mode = $RecognitionMode
    attempt = $Attempt
    idempotency_key = $IdempotencyKey
    files = @($resolvedFiles | ForEach-Object { [System.IO.Path]::GetFileName($_) })
    callback_configured = -not [string]::IsNullOrWhiteSpace($CallbackUrl)
    submitted_at = (Get-Date).ToString('o')
}
Write-JsonFile -Value $requestMetadata -Path (Join-Path $runDirectory 'request-metadata.json')

$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [System.Threading.Timeout]::InfiniteTimeSpan
$client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $apiKey)
$client.DefaultRequestHeaders.Add('Idempotency-Key', $IdempotencyKey)

try {
    if (-not $SkipHealthCheck) {
        $healthResponse = $client.GetAsync("$normalizedBaseUrl/health/ready").GetAwaiter().GetResult()
        $health = Read-JsonResponse -Response $healthResponse
        if (-not $healthResponse.IsSuccessStatusCode -or $health.status -ne 'ready') {
            throw "Service is not ready (HTTP $([int]$healthResponse.StatusCode))."
        }
        Write-Host 'Health check passed.'
    }

    $multipart = [System.Net.Http.MultipartFormDataContent]::new()
    $streams = [System.Collections.Generic.List[System.IDisposable]]::new()
    try {
        foreach ($resolvedFile in $resolvedFiles) {
            $stream = [System.IO.File]::OpenRead($resolvedFile)
            $streams.Add($stream)
            $fileContent = [System.Net.Http.StreamContent]::new($stream)
            $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::new('application/octet-stream')
            $multipart.Add($fileContent, 'files', [System.IO.Path]::GetFileName($resolvedFile))
        }
        $multipart.Add([System.Net.Http.StringContent]::new($MailSubject), 'mail_subject')
        $multipart.Add([System.Net.Http.StringContent]::new($RecognitionMode), 'recognition_mode')
        if (-not [string]::IsNullOrWhiteSpace($CallbackUrl)) {
            $multipart.Add([System.Net.Http.StringContent]::new($CallbackUrl), 'callback_url')
        }

        Write-Host "Submitting $CaseId in $RecognitionMode mode..."
        $submitResponse = $client.PostAsync("$normalizedBaseUrl/api/v1/extract/async/upload", $multipart).GetAwaiter().GetResult()
        $submit = Read-JsonResponse -Response $submitResponse
        Write-JsonFile -Value $submit -Path (Join-Path $runDirectory 'submit-response.json')
        if (-not $submitResponse.IsSuccessStatusCode) {
            throw "Submission failed (HTTP $([int]$submitResponse.StatusCode)). See submit-response.json."
        }
        if ([string]::IsNullOrWhiteSpace([string]$submit.task_id)) {
            throw 'Submission response did not contain task_id.'
        }
        $taskId = [string]$submit.task_id
        Write-Host "Task ID: $taskId"
    }
    finally {
        if ($null -ne $multipart) { $multipart.Dispose() }
        foreach ($stream in $streams) { $stream.Dispose() }
    }

    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    do {
        $taskResponse = $client.GetAsync("$normalizedBaseUrl/api/v1/tasks/$taskId").GetAwaiter().GetResult()
        $task = Read-JsonResponse -Response $taskResponse
        if (-not $taskResponse.IsSuccessStatusCode) {
            Write-JsonFile -Value $task -Path (Join-Path $runDirectory 'task-query-error.json')
            throw "Task query failed (HTTP $([int]$taskResponse.StatusCode))."
        }
        Write-Host "Status: $($task.status)"
        if ($task.status -notin @('PENDING', 'PROCESSING')) { break }
        if ((Get-Date) -ge $deadline) {
            Write-JsonFile -Value $task -Path (Join-Path $runDirectory 'task-result.json')
            Write-Warning "Polling timed out. Keep task_id $taskId and query it later; do not submit a new task yet."
            exit 3
        }
        Start-Sleep -Seconds $PollIntervalSeconds
    } while ($true)

    Write-JsonFile -Value $task -Path (Join-Path $runDirectory 'task-result.json')
    if ($task.status -eq 'SUCCESS') {
        Write-JsonFile -Value $task.result_json -Path (Join-Path $runDirectory 'extracted-result.json')
        Write-Host "SUCCESS. Result saved to $runDirectory"
        exit 0
    }

    if ($task.status -eq 'FAILED') {
        Write-Error "Task failed: $($task.error_message)"
        exit 2
    }

    Write-Warning "Task ended with unexpected status: $($task.status)"
    exit 4
}
finally {
    $client.Dispose()
}
