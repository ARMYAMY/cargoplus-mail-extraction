[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$TaskId,

    [Parameter(Mandatory = $true)]
    [string]$CorrectedJsonPath,

    [ValidateLength(0, 1000)]
    [string]$Notes = '',

    [Parameter(Mandatory = $true)]
    [switch]$BusinessConfirmed,

    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'feedback-results')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

if (-not $BusinessConfirmed) {
    throw 'Feedback can only be submitted after business confirmation. Add -BusinessConfirmed.'
}

$apiKey = $env:CARGOPLUS_API_KEY
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw 'Environment variable CARGOPLUS_API_KEY is not set.'
}
if (-not (Test-Path -LiteralPath $CorrectedJsonPath -PathType Leaf)) {
    throw "Corrected JSON file not found: $CorrectedJsonPath"
}

$correctedText = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $CorrectedJsonPath).Path, [System.Text.Encoding]::UTF8)
try {
    $correctedResult = $correctedText | ConvertFrom-Json
}
catch {
    throw "Corrected JSON is invalid: $($_.Exception.Message)"
}
if ($null -eq $correctedResult -or $correctedResult -isnot [psobject]) {
    throw 'Corrected JSON must be an object.'
}

$payload = [ordered]@{
    corrected_result = $correctedResult
    notes = $Notes
}
$payloadText = $payload | ConvertTo-Json -Depth 100
$normalizedBaseUrl = $BaseUrl.TrimEnd('/')

$client = [System.Net.Http.HttpClient]::new()
$client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $apiKey)
try {
    $content = [System.Net.Http.StringContent]::new($payloadText, [System.Text.Encoding]::UTF8, 'application/json')
    $response = $client.PostAsync("$normalizedBaseUrl/api/v1/tasks/$TaskId/feedback", $content).GetAwaiter().GetResult()
    $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    try { $responseObject = $responseText | ConvertFrom-Json } catch { $responseObject = $responseText }

    [System.IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
    $safeTaskId = $TaskId -replace '[^A-Za-z0-9._-]', '_'
    $outputPath = Join-Path $OutputDirectory "$safeTaskId-feedback-response.json"
    [System.IO.File]::WriteAllText(
        $outputPath,
        ($responseObject | ConvertTo-Json -Depth 100),
        [System.Text.UTF8Encoding]::new($false)
    )

    if (-not $response.IsSuccessStatusCode) {
        throw "Feedback submission failed (HTTP $([int]$response.StatusCode)). See $outputPath"
    }
    Write-Host "Feedback submitted. Response saved to $outputPath"
    $responseObject | ConvertTo-Json -Depth 100
}
finally {
    $client.Dispose()
}
