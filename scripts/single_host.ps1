param(
    [ValidateSet("Deploy", "Start", "Stop", "Status", "Logs", "Backup", "RestoreDrill")]
    [string]$Action = "Deploy",
    [switch]$SkipImageScan
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$serviceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$composeFile = Join-Path $serviceRoot "docker-compose.single-host.yml"
$singleHostDir = Join-Path $serviceRoot "deploy\single-host"
$secretDir = Join-Path $singleHostDir "secrets"
$singleHostEnv = Join-Path $singleHostDir ".env"
$legacyEnv = Join-Path $serviceRoot ".env"
$backupDir = Join-Path $serviceRoot "data\backups"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-NativeSuccess([string]$Message) {
    if ($LASTEXITCODE -ne 0) {
        throw $Message
    }
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $values
    }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            continue
        }
        $name = $matches[1]
        $value = $matches[2].Trim()
        if ($value.Length -ge 2) {
            if (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or
                ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $values[$name] = $value
    }
    return $values
}

function Set-DotEnvValue(
    [string]$Path,
    [string]$Name,
    [string]$Value,
    [switch]$OnlyIfMissing
) {
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($Path)) { [void]$lines.Add($line) }
    }
    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match "^\s*$([Regex]::Escape($Name))\s*=") {
            $found = $true
            if (-not $OnlyIfMissing) { $lines[$index] = "$Name=$Value" }
            break
        }
    }
    if (-not $found) { [void]$lines.Add("$Name=$Value") }
    [IO.File]::WriteAllLines($Path, $lines, $utf8NoBom)
}

function Get-LanIPv4 {
    $config = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" -and
            $_.IPv4Address.IPAddress -notlike "169.254*"
        } |
        Select-Object -First 1
    if (-not $config -or -not $config.IPv4Address.IPAddress) {
        throw "No active LAN IPv4 address with a default gateway was found."
    }
    return [string]($config.IPv4Address.IPAddress | Select-Object -First 1)
}

function New-RandomSecret([int]$Bytes = 36) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    } finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Write-SecretIfMissing([string]$Name, [string]$Value) {
    $path = Join-Path $secretDir $Name
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $existing = [IO.File]::ReadAllText($path).Trim()
        if ([string]::IsNullOrWhiteSpace($existing)) {
            throw "Secret file is empty: $path"
        }
        return $existing
    }
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "Refusing to write an empty or multiline secret: $Name"
    }
    [IO.File]::WriteAllText($path, $Value, $utf8NoBom)
    return $Value
}

function Get-SecurePromptValue([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Get-CurrentPostgresEnvironment {
    $result = @{}
    $containerId = (& docker ps --filter "label=com.docker.compose.project=cargoplus" --filter "label=com.docker.compose.service=postgres" --format "{{.ID}}" 2>$null | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($containerId)) {
        return $result
    }
    $lines = & docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' $containerId 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $result
    }
    foreach ($line in $lines) {
        if ($line -match '^(POSTGRES_DB|POSTGRES_USER|POSTGRES_PASSWORD)=(.*)$') {
            $result[$matches[1]] = $matches[2]
        }
    }
    return $result
}

function Test-DockerVolume([string]$Name) {
    & docker volume inspect $Name *> $null
    return $LASTEXITCODE -eq 0
}

function Invoke-Compose([string[]]$Arguments) {
    & docker compose --env-file $singleHostEnv -f $composeFile @Arguments
    Assert-NativeSuccess "Docker Compose command failed: $($Arguments -join ' ')"
}

function Assert-DockerReady {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI was not found. Install and start Docker Desktop first."
    }
    & docker compose version *> $null
    Assert-NativeSuccess "Docker Compose v2 is not available."
    & docker version --format '{{.Server.Version}}' *> $null
    Assert-NativeSuccess "Docker Desktop is not running."
}

function Initialize-SingleHostConfiguration {
    New-Item -ItemType Directory -Force -Path $singleHostDir, $secretDir, $backupDir | Out-Null

    $legacy = Read-DotEnv $legacyEnv
    $saved = Read-DotEnv $singleHostEnv
    $postgresContainer = Get-CurrentPostgresEnvironment
    $postgresVolumeExists = Test-DockerVolume "cargoplus_postgres_data"
    $httpsHost = if ($saved.ContainsKey("SINGLE_HOST_HTTPS_HOST")) {
        $saved["SINGLE_HOST_HTTPS_HOST"]
    } else {
        Get-LanIPv4
    }

    $postgresUser = "cargo"
    $postgresDb = "cargo"
    if ($saved.ContainsKey("POSTGRES_USER")) { $postgresUser = $saved["POSTGRES_USER"] }
    elseif ($postgresContainer.ContainsKey("POSTGRES_USER")) { $postgresUser = $postgresContainer["POSTGRES_USER"] }
    elseif ($legacy.ContainsKey("POSTGRES_USER")) { $postgresUser = $legacy["POSTGRES_USER"] }
    if ($saved.ContainsKey("POSTGRES_DB")) { $postgresDb = $saved["POSTGRES_DB"] }
    elseif ($postgresContainer.ContainsKey("POSTGRES_DB")) { $postgresDb = $postgresContainer["POSTGRES_DB"] }
    elseif ($legacy.ContainsKey("POSTGRES_DB")) { $postgresDb = $legacy["POSTGRES_DB"] }

    $postgresPasswordCandidate = ""
    if ($postgresContainer.ContainsKey("POSTGRES_PASSWORD")) {
        $postgresPasswordCandidate = $postgresContainer["POSTGRES_PASSWORD"]
    } elseif ($legacy.ContainsKey("POSTGRES_PASSWORD")) {
        $postgresPasswordCandidate = $legacy["POSTGRES_PASSWORD"]
    } elseif ($postgresVolumeExists) {
        # This is the previous docker-compose.yml default and preserves an existing local volume.
        $postgresPasswordCandidate = "cargo-local-password"
    } else {
        $postgresPasswordCandidate = New-RandomSecret
    }
    $postgresPassword = Write-SecretIfMissing "postgres_password" $postgresPasswordCandidate

    $redisPassword = Write-SecretIfMissing "redis_password" (New-RandomSecret)
    $encodedPgUser = [Uri]::EscapeDataString($postgresUser)
    $encodedPgPassword = [Uri]::EscapeDataString($postgresPassword)
    $encodedPgDb = [Uri]::EscapeDataString($postgresDb)
    $encodedRedisPassword = [Uri]::EscapeDataString($redisPassword)
    [void](Write-SecretIfMissing "database_url" "postgresql+asyncpg://${encodedPgUser}:${encodedPgPassword}@postgres:5432/${encodedPgDb}")
    [void](Write-SecretIfMissing "postgres_backup_url" "postgresql://${encodedPgUser}:${encodedPgPassword}@postgres:5432/${encodedPgDb}")
    [void](Write-SecretIfMissing "celery_broker_url" "redis://:${encodedRedisPassword}@redis:6379/0")
    [void](Write-SecretIfMissing "celery_result_backend" "redis://:${encodedRedisPassword}@redis:6379/1")

    $adminCandidate = if ($legacy.ContainsKey("ADMIN_SECRET_KEY")) { $legacy["ADMIN_SECRET_KEY"] } else { New-RandomSecret 48 }
    $sessionCandidate = if ($legacy.ContainsKey("SESSION_SECRET_KEY")) { $legacy["SESSION_SECRET_KEY"] } else { New-RandomSecret 48 }
    [void](Write-SecretIfMissing "admin_secret" $adminCandidate)
    [void](Write-SecretIfMissing "session_secret" $sessionCandidate)
    [void](Write-SecretIfMissing "grafana_admin_password" (New-RandomSecret 24))
    [void](Write-SecretIfMissing "restore_drill_password" (New-RandomSecret 24))

    $llmSecretPath = Join-Path $secretDir "llm_api_key"
    if (-not (Test-Path -LiteralPath $llmSecretPath -PathType Leaf)) {
        $llmCandidate = if ($legacy.ContainsKey("LLM_API_KEY")) { $legacy["LLM_API_KEY"] } else { "" }
        if ([string]::IsNullOrWhiteSpace($llmCandidate)) {
            $llmCandidate = Get-SecurePromptValue "请输入 LLM API Key（输入内容不会显示）"
        }
        [void](Write-SecretIfMissing "llm_api_key" $llmCandidate)
    } else {
        [void](Write-SecretIfMissing "llm_api_key" "unused")
    }

    if (-not (Test-Path -LiteralPath $singleHostEnv -PathType Leaf)) {
        $backupPathForCompose = (Resolve-Path -LiteralPath $backupDir).Path.Replace('\', '/')
        $llmBaseUrl = if ($legacy.ContainsKey("LLM_BASE_URL")) { $legacy["LLM_BASE_URL"] } else { "https://api.openai.com/v1" }
        $llmModel = if ($legacy.ContainsKey("LLM_MODEL")) { $legacy["LLM_MODEL"] } else { "gpt-5-mini" }
        $llmFallback = if ($legacy.ContainsKey("LLM_FALLBACK_MODEL")) { $legacy["LLM_FALLBACK_MODEL"] } else { $llmModel }
        $visionEnabled = if ($legacy.ContainsKey("VISION_LLM_ENABLED")) { $legacy["VISION_LLM_ENABLED"] } else { "false" }
        $visionModel = if ($legacy.ContainsKey("VISION_LLM_MODEL")) { $legacy["VISION_LLM_MODEL"] } else { "qwen3.8-27b" }
        $lines = @(
            "CARGOPLUS_IMAGE=cargoplus-app:single-host",
            "HTTP_PORT=80",
            "HTTPS_PORT=443",
            "SINGLE_HOST_HTTPS_HOST=$httpsHost",
            "GRAFANA_ADMIN_USER=admin",
            "POSTGRES_USER=$postgresUser",
            "POSTGRES_DB=$postgresDb",
            "SINGLE_HOST_BACKUP_DIR=$backupPathForCompose",
            "BACKUP_RETENTION_DAYS=14",
            "BACKUP_INTERVAL_SECONDS=86400",
            "CELERY_WORKER_CONCURRENCY=4",
            "CELERY_WEBHOOK_CONCURRENCY=2",
            "DEFAULT_TENANT_CONCURRENCY=4",
            "MAX_TENANT_PENDING_TASKS=500",
            "MAX_GLOBAL_PENDING_TASKS=5000",
            "TASK_LEASE_SECONDS=600",
            "TASK_RECOVERY_INTERVAL_SECONDS=30",
            "BEAT_LOCK_TTL_SECONDS=25",
            "LLM_BASE_URL=$llmBaseUrl",
            "LLM_MODEL=$llmModel",
            "LLM_FALLBACK_MODEL=$llmFallback",
            "VISION_LLM_ENABLED=$visionEnabled",
            "VISION_LLM_MODEL=$visionModel",
            "VISION_LLM_TIMEOUT_SECONDS=30",
            "VISION_MAX_IMAGES_PER_TASK=5",
            "CORS_ALLOWED_ORIGINS=https://$httpsHost"
        )
        [IO.File]::WriteAllLines($singleHostEnv, $lines, $utf8NoBom)
    }
    Set-DotEnvValue $singleHostEnv "HTTP_PORT" "80" -OnlyIfMissing
    Set-DotEnvValue $singleHostEnv "HTTPS_PORT" "443" -OnlyIfMissing
    Set-DotEnvValue $singleHostEnv "SINGLE_HOST_HTTPS_HOST" $httpsHost -OnlyIfMissing
    Set-DotEnvValue $singleHostEnv "VISION_LLM_ENABLED" "false" -OnlyIfMissing
    Set-DotEnvValue $singleHostEnv "VISION_LLM_MODEL" "qwen3.8-27b" -OnlyIfMissing
    Set-DotEnvValue $singleHostEnv "VISION_LLM_TIMEOUT_SECONDS" "30" -OnlyIfMissing
    Set-DotEnvValue $singleHostEnv "VISION_MAX_IMAGES_PER_TASK" "5" -OnlyIfMissing
    # Upgrade the previous loopback HTTP deployment to the single public HTTPS origin.
    Set-DotEnvValue $singleHostEnv "CORS_ALLOWED_ORIGINS" "https://$httpsHost"
}

function Wait-ForHttp([string]$Url, [string]$Name, [int]$TimeoutMinutes = 4) {
    $deadline = [DateTime]::UtcNow.AddMinutes($TimeoutMinutes)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 4
            if ($response.StatusCode -eq 200) {
                return
            }
        } catch {
        }
        Start-Sleep -Seconds 3
    }
    Invoke-Compose @("ps")
    throw "$Name did not become ready within $TimeoutMinutes minutes: $Url"
}

function Wait-ForHttps([string]$Url, [int]$TimeoutMinutes = 4) {
    $deadline = [DateTime]::UtcNow.AddMinutes($TimeoutMinutes)
    while ([DateTime]::UtcNow -lt $deadline) {
        # Private CAs have no public CRL endpoint; certificate trust and hostname
        # validation remain enabled while only the unavailable revocation lookup is skipped.
        & curl.exe --noproxy "*" --ssl-no-revoke --fail --silent --show-error $Url *> $null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 3
    }
    Invoke-Compose @("logs", "--tail=100", "caddy")
    throw "CargoPlus HTTPS did not become ready within $TimeoutMinutes minutes: $Url"
}

function Get-ConfigValue([string]$Name, [string]$Default) {
    $values = Read-DotEnv $singleHostEnv
    if ($values.ContainsKey($Name)) {
        return [string]$values[$Name]
    }
    return $Default
}

function Wait-ForContainer([string]$Service, [int]$TimeoutMinutes = 4) {
    $deadline = [DateTime]::UtcNow.AddMinutes($TimeoutMinutes)
    while ([DateTime]::UtcNow -lt $deadline) {
        $containerId = (& docker compose --env-file $singleHostEnv -f $composeFile ps -q $Service 2>$null | Select-Object -First 1)
        if (-not [string]::IsNullOrWhiteSpace($containerId)) {
            $status = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $containerId 2>$null)
            if ($status -eq "healthy" -or $status -eq "running") { return }
            if ($status -eq "unhealthy" -or $status -eq "exited" -or $status -eq "dead") {
                Invoke-Compose @("logs", "--tail=100", $Service)
                throw "$Service entered terminal state: $status"
            }
        }
        Start-Sleep -Seconds 3
    }
    throw "$Service did not become healthy within $TimeoutMinutes minutes."
}

function Export-AndTrustCaddyRoot {
    $rootCertificate = Join-Path $singleHostDir "caddy-root.crt"
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    $copied = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        $containerId = (& docker compose --env-file $singleHostEnv -f $composeFile ps -q caddy 2>$null | Select-Object -First 1)
        if (-not [string]::IsNullOrWhiteSpace($containerId)) {
            & docker cp "${containerId}:/data/caddy/pki/authorities/local/root.crt" $rootCertificate *> $null
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $rootCertificate -PathType Leaf)) {
                $copied = $true
                break
            }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $copied) { throw "Caddy internal CA root certificate was not generated." }

    $certificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2($rootCertificate)
    $trusted = Get-ChildItem Cert:\CurrentUser\Root |
        Where-Object { $_.Thumbprint -eq $certificate.Thumbprint } |
        Select-Object -First 1
    if (-not $trusted) {
        & certutil.exe -user -addstore -f Root $rootCertificate *> $null
        Assert-NativeSuccess "Failed to trust the Caddy root certificate for the current Windows user."
    }
}

Assert-DockerReady

if ($Action -eq "Deploy") {
    Write-Step "初始化单机配置（不会覆盖已有密钥或数据卷）"
    Initialize-SingleHostConfiguration

    Write-Step "校验 Compose 配置"
    Invoke-Compose @("config", "--quiet")

    Write-Step "构建 CargoPlus 应用镜像"
    Invoke-Compose @("build", "api")

    if (-not $SkipImageScan) {
        Write-Step "扫描镜像高危漏洞、密钥和错误配置"
        & (Join-Path $PSScriptRoot "scan_image.ps1") -Image "cargoplus-app:single-host"
        Assert-NativeSuccess "Container image scan failed."
    }

    Write-Step "启动 PostgreSQL、Redis、API、Celery、监控和备份服务"
    Invoke-Compose @("up", "-d", "--no-build", "--remove-orphans")
    $httpsHost = Get-ConfigValue "SINGLE_HOST_HTTPS_HOST" ""
    $httpsPort = [int](Get-ConfigValue "HTTPS_PORT" "443")
    Wait-ForContainer "api" 4
    Wait-ForContainer "grafana" 8
    Wait-ForContainer "prometheus" 2
    Wait-ForContainer "alertmanager" 2
    Wait-ForContainer "caddy" 2
    Write-Step "安装本机 HTTPS 根证书"
    Export-AndTrustCaddyRoot
    $publicUrl = if ($httpsPort -eq 443) { "https://$httpsHost" } else { "https://${httpsHost}:$httpsPort" }
    Wait-ForHttps "$publicUrl/health/ready" 4
    $prometheusId = (& docker compose --env-file $singleHostEnv -f $composeFile ps -q prometheus | Select-Object -First 1)
    & docker exec $prometheusId wget -qO- --post-data= http://127.0.0.1:9090/-/reload *> $null
    Assert-NativeSuccess "Prometheus rejected its updated alert configuration."

    Write-Step "部署完成"
    Invoke-Compose @("ps")
    Write-Host "CargoPlus: $publicUrl" -ForegroundColor Green
    Write-Host "Grafana、Prometheus、Alertmanager 仅在 Docker 内部网络监听，不对外开放。"
    Write-Host "局域网其他设备需信任: deploy\single-host\caddy-root.crt"
    Write-Host "数据库备份目录: $backupDir"
    exit 0
}

if (-not (Test-Path -LiteralPath $singleHostEnv -PathType Leaf)) {
    throw "Single-host configuration is missing. Run Deploy first."
}

switch ($Action) {
    "Start" {
        Write-Step "启动单机服务"
        Invoke-Compose @("up", "-d", "--no-build")
    }
    "Stop" {
        Write-Step "停止单机服务（保留所有数据卷）"
        Invoke-Compose @("stop")
    }
    "Status" {
        Invoke-Compose @("ps")
    }
    "Logs" {
        Invoke-Compose @("logs", "--tail=200", "-f")
    }
    "Backup" {
        Write-Step "执行一次校验过的 PostgreSQL 与 Redis 备份"
        Invoke-Compose @("run", "--rm", "-e", "BACKUP_RUN_ONCE=true", "postgres-backup")
        Invoke-Compose @("run", "--rm", "-e", "BACKUP_RUN_ONCE=true", "redis-backup")
    }
    "RestoreDrill" {
        Write-Step "在一次性 PostgreSQL 实例中执行恢复演练"
        & docker compose --env-file $singleHostEnv -f $composeFile --profile restore-drill rm -s -f restore-drill restore-db *> $null
        try {
            Invoke-Compose @("--profile", "restore-drill", "up", "--abort-on-container-exit", "--exit-code-from", "restore-drill", "restore-db", "restore-drill")
        } finally {
            & docker compose --env-file $singleHostEnv -f $composeFile --profile restore-drill rm -s -f restore-drill restore-db *> $null
        }
    }
}
