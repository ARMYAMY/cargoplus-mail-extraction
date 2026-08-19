param(
    [string]$Image = "cargoplus-app:production",
    [string]$TrivyImage = "aquasec/trivy:0.69.3"
)

$ErrorActionPreference = "Stop"
docker image inspect $Image *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Image does not exist locally: $Image"
}

$reportDir = Join-Path $PSScriptRoot "..\reports"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$resolvedReportDir = (Resolve-Path -LiteralPath $reportDir).Path

docker run --rm `
    -v /var/run/docker.sock:/var/run/docker.sock `
    -v cargoplus_trivy_cache:/root/.cache/trivy `
    -v "${resolvedReportDir}:/reports" `
    $TrivyImage image `
    --scanners vuln,secret,misconfig `
    --severity HIGH,CRITICAL `
    --ignore-unfixed `
    --exit-code 1 `
    --format table `
    $Image
if ($LASTEXITCODE -ne 0) {
    throw "Trivy found blocking vulnerabilities or secrets"
}

docker run --rm `
    -v /var/run/docker.sock:/var/run/docker.sock `
    -v cargoplus_trivy_cache:/root/.cache/trivy `
    -v "${resolvedReportDir}:/reports" `
    $TrivyImage image `
    --format cyclonedx `
    --output /reports/cargoplus-sbom.cdx.json `
    $Image
if ($LASTEXITCODE -ne 0) {
    throw "SBOM generation failed"
}

Write-Output "Image scan passed; SBOM: $resolvedReportDir\cargoplus-sbom.cdx.json"
