#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.cloud.yml"
CLOUD_DIR="$PROJECT_DIR/deploy/cloud"
SECRETS_DIR="$CLOUD_DIR/secrets"
ENV_FILE="$CLOUD_DIR/.env"
STATE_DIR="/opt/cargoplus"
BACKUP_DIR="$STATE_DIR/backups"
CA_EXPORT="$STATE_DIR/caddy-root.crt"
CREDENTIALS_FILE="$STATE_DIR/admin-credentials.txt"
TRIVY_CACHE_DIR="$STATE_DIR/trivy-cache"

ACTION="deploy"
TLS_MODE=""
PUBLIC_HOST_INPUT=""
LLM_KEY_FILE=""
ENABLE_FIREWALL="true"
RUN_SCAN="true"

usage() {
  cat <<'EOF'
CargoPlus cloud single-server deployment

Usage:
  sudo bash scripts/cloud_deploy.sh deploy [--domain example.com | --ip 203.0.113.10]
       [--llm-key-file /root/llm-key] [--no-firewall] [--skip-scan]
  sudo bash scripts/cloud_deploy.sh status
  sudo bash scripts/cloud_deploy.sh logs [service]
  sudo bash scripts/cloud_deploy.sh backup
  sudo bash scripts/cloud_deploy.sh restore-drill
  sudo bash scripts/cloud_deploy.sh upgrade [--skip-scan]
  sudo bash scripts/cloud_deploy.sh export-ca

Without --domain or --ip, deploy auto-detects the public IPv4 address. Domain
mode obtains a publicly trusted certificate. IP mode uses a private CA and
exports /opt/cargoplus/caddy-root.crt for installation on client devices.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "run with sudo or as root"
}

validate_ipv4() {
  local ip="$1" IFS=. part count=0
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  read -r -a parts <<<"$ip"
  for part in "${parts[@]}"; do
    [ "$part" -ge 0 ] 2>/dev/null && [ "$part" -le 255 ] || return 1
    count=$((count + 1))
  done
  [ "$count" -eq 4 ]
}

validate_domain() {
  local value="$1"
  [ "${#value}" -le 253 ] || return 1
  [[ "$value" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}

random_hex() {
  openssl rand -hex "$1"
}

write_secret() {
  local target="$1" value="$2" temporary
  temporary="$(mktemp "$SECRETS_DIR/.secret.XXXXXX")"
  printf '%s\n' "$value" >"$temporary"
  # Compose file secrets are bind mounts on Linux. The parent directory is
  # root-only (0700), while the mounted file must remain readable by the
  # different non-root UIDs used by app/PostgreSQL/Redis containers.
  chmod 0644 "$temporary"
  mv -f "$temporary" "$target"
}

ensure_random_secret() {
  local name="$1" bytes="$2"
  if [ ! -s "$SECRETS_DIR/$name" ]; then
    write_secret "$SECRETS_DIR/$name" "$(random_hex "$bytes")"
  fi
}

compose() {
  docker compose --project-directory "$PROJECT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    systemctl enable --now docker >/dev/null 2>&1 || true
    return
  fi

  command -v apt-get >/dev/null 2>&1 || die "automatic Docker installation supports Ubuntu/Debian only"
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) ;;
    *) die "automatic Docker installation supports Ubuntu/Debian only (found ${ID:-unknown})" ;;
  esac

  log "Installing Docker Engine from Docker's signed APT repository"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg openssl ufw
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  local codename
  codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  [ -n "$codename" ] || die "cannot determine the OS release codename"
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/%s %s stable\n' \
    "$(dpkg --print-architecture)" "$ID" "$codename" >/etc/apt/sources.list.d/docker.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

ensure_host_tools() {
  local missing="false" command_name
  for command_name in curl openssl getent awk find; do
    command -v "$command_name" >/dev/null 2>&1 || missing="true"
  done
  if [ "$missing" = "false" ]; then
    return 0
  fi
  command -v apt-get >/dev/null 2>&1 || die "curl, openssl and standard GNU tools are required"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl openssl libc-bin gawk findutils
}

configure_firewall() {
  if [ "$ENABLE_FIREWALL" != "true" ]; then
    log "Firewall setup skipped by request"
    return 0
  fi
  command -v ufw >/dev/null 2>&1 || {
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ufw
  }
  local ssh_port
  ssh_port="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')"
  ssh_port="${ssh_port:-22}"
  [[ "$ssh_port" =~ ^[0-9]+$ ]] || die "unable to determine a safe SSH port"
  ufw allow "$ssh_port/tcp" comment 'SSH' >/dev/null
  ufw allow 30010/tcp comment 'CargoPlus Web 30010' >/dev/null
  ufw --force enable >/dev/null
  log "UFW enabled: SSH $ssh_port/tcp, CargoPlus Web 30010/tcp"
}

detect_public_ipv4() {
  local candidate=""
  candidate="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  if validate_ipv4 "$candidate"; then
    printf '%s' "$candidate"
    return 0
  fi
  candidate="$(hostname -I 2>/dev/null | awk '{print $1}')"
  validate_ipv4 "$candidate" || return 1
  printf '%s' "$candidate"
}

check_host_prerequisites() {
  [ -f "$COMPOSE_FILE" ] || die "missing $COMPOSE_FILE"
  [ -d "$PROJECT_DIR/app" ] || die "run this script from the complete CargoPlus source package"
  [ -d "$(dirname "$PROJECT_DIR")/cargo-mail-extraction-skill-v3" ] || \
    die "cargo-mail-extraction-skill-v3 must be next to the cargo_service directory"

  local memory_mb disk_gb
  memory_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
  disk_gb="$(df -Pk "$PROJECT_DIR" | awk 'NR==2 {print int($4/1024/1024)}')"
  [ "$memory_mb" -ge 3800 ] || log "WARNING: ${memory_mb}MB RAM detected; 8GB is recommended for OCR workers"
  [ "$disk_gb" -ge 15 ] || log "WARNING: only ${disk_gb}GB free; at least 20GB is recommended"
}

select_public_host() {
  if [ -n "$PUBLIC_HOST_INPUT" ]; then
    if [ "$TLS_MODE" = "domain" ]; then
      validate_domain "$PUBLIC_HOST_INPUT" || die "invalid domain: $PUBLIC_HOST_INPUT"
    else
      validate_ipv4 "$PUBLIC_HOST_INPUT" || die "--ip requires a valid IPv4 address"
    fi
    return 0
  fi
  TLS_MODE="ip"
  PUBLIC_HOST_INPUT="$(detect_public_ipv4)" || die "cannot detect public IPv4; rerun with --ip or --domain"
  log "Detected public IPv4: $PUBLIC_HOST_INPUT"
}

check_domain_dns() {
  if [ "$TLS_MODE" != "domain" ]; then
    return 0
  fi
  local detected resolved
  detected="$(detect_public_ipv4 || true)"
  resolved="$(getent ahostsv4 "$PUBLIC_HOST_INPUT" 2>/dev/null | awk '{print $1}' | sort -u)"
  [ -n "$resolved" ] || die "domain does not have an IPv4 DNS record yet: $PUBLIC_HOST_INPUT"
  if [ -n "$detected" ] && ! grep -Fxq "$detected" <<<"$resolved"; then
    die "DNS for $PUBLIC_HOST_INPUT does not point to this server ($detected); update DNS before deploying"
  fi
}

prepare_configuration() {
  install -d -m 0700 "$STATE_DIR" "$BACKUP_DIR" "$TRIVY_CACHE_DIR" "$SECRETS_DIR"
  ensure_random_secret postgres_password 32
  ensure_random_secret redis_password 32
  ensure_random_secret admin_secret 48
  ensure_random_secret session_secret 48
  ensure_random_secret restore_drill_password 32

  local postgres_password redis_password
  postgres_password="$(tr -d '\r\n' <"$SECRETS_DIR/postgres_password")"
  redis_password="$(tr -d '\r\n' <"$SECRETS_DIR/redis_password")"
  write_secret "$SECRETS_DIR/database_url" "postgresql+asyncpg://cargo:${postgres_password}@postgres:5432/cargo"
  write_secret "$SECRETS_DIR/postgres_backup_url" "postgresql://cargo:${postgres_password}@postgres:5432/cargo"
  write_secret "$SECRETS_DIR/celery_broker_url" "redis://:${redis_password}@redis:6379/0"
  write_secret "$SECRETS_DIR/celery_result_backend" "redis://:${redis_password}@redis:6379/1"

  if [ ! -s "$SECRETS_DIR/llm_api_key" ]; then
    local llm_key=""
    if [ -n "$LLM_KEY_FILE" ]; then
      [ -r "$LLM_KEY_FILE" ] || die "LLM key file is not readable"
      [ "$(wc -c <"$LLM_KEY_FILE")" -le 65536 ] || die "LLM key file is unexpectedly large"
      llm_key="$(tr -d '\r\n' <"$LLM_KEY_FILE")"
    elif [ -t 0 ]; then
      read -r -s -p "LLM API Key: " llm_key
      echo
    else
      die "first deployment needs --llm-key-file in non-interactive mode"
    fi
    [ -n "$llm_key" ] || die "LLM API key must not be empty"
    write_secret "$SECRETS_DIR/llm_api_key" "$llm_key"
  fi
  find "$SECRETS_DIR" -maxdepth 1 -type f -exec chmod 0644 {} +

  local llm_base_url llm_model template
  llm_base_url="${LLM_BASE_URL:-https://api.senseaudio.cn/v1}"
  [[ "$llm_base_url" == https://* ]] || die "LLM_BASE_URL must use HTTPS"
  llm_model="${LLM_MODEL:-deepseek-v4-flash-0731}"
  [[ "$llm_model" =~ ^[A-Za-z0-9._:/-]+$ ]] || die "LLM_MODEL contains unsupported characters"

  template="$CLOUD_DIR/Caddyfile.$TLS_MODE"
  [ -f "$template" ] || die "missing Caddy template: $template"
  install -m 0600 "$template" "$CLOUD_DIR/Caddyfile.active"

  cat >"$ENV_FILE" <<EOF
PUBLIC_HOST=$PUBLIC_HOST_INPUT
APP_PORT=30010
TLS_MODE=$TLS_MODE
ACME_EMAIL=${ACME_EMAIL:-}
CORS_ALLOWED_ORIGINS=http://$PUBLIC_HOST_INPUT:30010,https://$PUBLIC_HOST_INPUT:30010,http://localhost:30010
ALLOWED_HOSTS=$PUBLIC_HOST_INPUT,api,localhost,127.0.0.1
CLOUD_BACKUP_DIR=$BACKUP_DIR
CARGOPLUS_IMAGE=cargoplus-app:cloud
CELERY_WORKER_CONCURRENCY=${CELERY_WORKER_CONCURRENCY:-20}
CELERY_WEBHOOK_CONCURRENCY=${CELERY_WEBHOOK_CONCURRENCY:-1}
DEFAULT_TENANT_CONCURRENCY=${DEFAULT_TENANT_CONCURRENCY:-20}
MAX_TENANT_PENDING_TASKS=${MAX_TENANT_PENDING_TASKS:-200}
MAX_GLOBAL_PENDING_TASKS=${MAX_GLOBAL_PENDING_TASKS:-1000}
BACKUP_RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
BACKUP_INTERVAL_SECONDS=86400
DATA_RETENTION_DAYS=${DATA_RETENTION_DAYS:-90}
AUTH_TOKEN_TTL_SECONDS=${AUTH_TOKEN_TTL_SECONDS:-28800}
LLM_BASE_URL=$llm_base_url
LLM_MODEL=$llm_model
LLM_FALLBACK_MODEL=${LLM_FALLBACK_MODEL:-}
LLM_TIMEOUT_SECONDS=${LLM_TIMEOUT_SECONDS:-120}
LLM_MAX_RETRIES=${LLM_MAX_RETRIES:-2}
VISION_LLM_ENABLED=${VISION_LLM_ENABLED:-true}
VISION_LLM_MODEL=${VISION_LLM_MODEL:-qwen3.8-27b}
VISION_LLM_TIMEOUT_SECONDS=${VISION_LLM_TIMEOUT_SECONDS:-30}
VISION_MAX_IMAGES_PER_TASK=${VISION_MAX_IMAGES_PER_TASK:-5}
APP_MEMORY_LIMIT=${APP_MEMORY_LIMIT:-2g}
APP_CPU_LIMIT=${APP_CPU_LIMIT:-2.0}
WORKER_MEMORY_LIMIT=${WORKER_MEMORY_LIMIT:-4g}
WORKER_CPU_LIMIT=${WORKER_CPU_LIMIT:-4.0}
POSTGRES_MEMORY_LIMIT=${POSTGRES_MEMORY_LIMIT:-2g}
POSTGRES_CPU_LIMIT=${POSTGRES_CPU_LIMIT:-2.0}
REDIS_MEMORY_LIMIT=${REDIS_MEMORY_LIMIT:-1g}
REDIS_CPU_LIMIT=${REDIS_CPU_LIMIT:-1.0}
EOF
  chmod 0600 "$ENV_FILE"

  cat >"$CREDENTIALS_FILE" <<EOF
CargoPlus URL: http://$PUBLIC_HOST_INPUT:30010/
Admin username: admin
Admin password: $(tr -d '\r\n' <"$SECRETS_DIR/admin_secret")
Port: 30010
EOF
  chmod 0600 "$CREDENTIALS_FILE"
}

validate_compose() {
  compose config --quiet
  docker run --rm \
    -v "$CLOUD_DIR/Caddyfile.active:/etc/caddy/Caddyfile:ro" \
    caddy:2-alpine \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
}

scan_application_image() {
  if [ "$RUN_SCAN" != "true" ]; then
    log "Image vulnerability scan skipped by request"
    return 0
  fi
  log "Scanning application image for HIGH/CRITICAL vulnerabilities and secrets"
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$TRIVY_CACHE_DIR:/root/.cache/trivy" \
    aquasec/trivy:0.69.3 image --scanners vuln,secret --severity HIGH,CRITICAL \
    --ignore-unfixed --exit-code 1 cargoplus-app:cloud
}

export_ca() {
  local caddy_id
  caddy_id="$(compose ps -q caddy)"
  [ -n "$caddy_id" ] || return 0
  local temporary
  temporary="$(mktemp "$STATE_DIR/.caddy-root.XXXXXX" 2>/dev/null || true)"
  if [ -n "$temporary" ] && docker cp "$caddy_id:/data/caddy/pki/authorities/local/root.crt" "$temporary" >/dev/null 2>&1; then
    chmod 0644 "$temporary"
    mv -f "$temporary" "$CA_EXPORT"
    log "Private CA certificate exported to $CA_EXPORT"
  fi
  return 0
}

wait_for_service() {
  local attempt port="${APP_PORT:-30010}"
  log "Waiting for service to become healthy on port $port..."
  for attempt in $(seq 1 60); do
    if curl -fsS --max-time 5 "http://127.0.0.1:${port}/health/ready" >/dev/null 2>&1 || \
       curl -fsS --max-time 5 "http://${PUBLIC_HOST_INPUT}:${port}/health/ready" >/dev/null 2>&1; then
      log "Service is live and ready on port $port!"
      return 0
    fi
    sleep 3
  done
  compose ps
  compose logs --tail=100 api caddy
  die "Health check did not become ready on port $port"
}

deploy_stack() {
  check_host_prerequisites
  install_docker
  ensure_host_tools
  select_public_host
  check_domain_dns
  configure_firewall
  prepare_configuration
  validate_compose
  log "Building CargoPlus application image"
  compose build api
  scan_application_image
  log "Starting PostgreSQL, Redis, API, Celery workers, Beat, backups and Caddy"
  compose up -d --remove-orphans
  if [ "$TLS_MODE" = "ip" ]; then
    export_ca || true
  fi
  wait_for_service
  compose ps
  log "Deployment complete: http://$PUBLIC_HOST_INPUT:${APP_PORT:-30010}/"
  log "Admin credentials: $CREDENTIALS_FILE (root-readable only)"
}

load_existing() {
  [ -s "$ENV_FILE" ] || die "deployment is not initialized; run deploy first"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  TLS_MODE="${TLS_MODE:?missing TLS_MODE in deployment environment}"
  PUBLIC_HOST_INPUT="${PUBLIC_HOST:?missing PUBLIC_HOST in deployment environment}"
}

run_backup() {
  load_existing
  compose run --rm -e BACKUP_RUN_ONCE=true postgres-backup
  compose run --rm -e BACKUP_RUN_ONCE=true redis-backup
  log "Verified backups written to $BACKUP_DIR"
}

run_restore_drill() {
  load_existing
  compose --profile restore-drill up -d restore-db
  local result=0
  compose --profile restore-drill run --rm restore-drill || result=$?
  compose --profile restore-drill stop restore-db >/dev/null || true
  compose --profile restore-drill rm -f restore-db >/dev/null || true
  [ "$result" -eq 0 ] || die "restore drill failed"
  log "Restore drill passed"
}

upgrade_stack() {
  load_existing
  install_docker
  ensure_host_tools
  run_backup
  install -m 0600 "$CLOUD_DIR/Caddyfile.$TLS_MODE" "$CLOUD_DIR/Caddyfile.active"
  validate_compose
  compose build --pull api
  scan_application_image
  compose up -d --remove-orphans
  wait_for_service
  log "Upgrade completed"
}

if [ "$#" -gt 0 ] && [[ "$1" != --* ]]; then
  ACTION="$1"
  shift
fi

LOG_SERVICE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain) [ "$#" -ge 2 ] || die "--domain needs a value"; TLS_MODE="domain"; PUBLIC_HOST_INPUT="$2"; shift 2 ;;
    --ip) [ "$#" -ge 2 ] || die "--ip needs a value"; TLS_MODE="ip"; PUBLIC_HOST_INPUT="$2"; shift 2 ;;
    --llm-key-file) [ "$#" -ge 2 ] || die "--llm-key-file needs a value"; LLM_KEY_FILE="$2"; shift 2 ;;
    --no-firewall) ENABLE_FIREWALL="false"; shift ;;
    --skip-scan) RUN_SCAN="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [ "$ACTION" = "logs" ] && [ -z "$LOG_SERVICE" ]; then LOG_SERVICE="$1"; shift
      else die "unknown argument: $1"
      fi
      ;;
  esac
done

require_root
case "$ACTION" in
  deploy) deploy_stack ;;
  status) load_existing; compose ps ;;
  logs) load_existing; if [ -n "$LOG_SERVICE" ]; then compose logs --tail=200 -f "$LOG_SERVICE"; else compose logs --tail=200 -f; fi ;;
  backup) run_backup ;;
  restore-drill) run_restore_drill ;;
  upgrade) upgrade_stack ;;
  export-ca) load_existing; [ "$TLS_MODE" = "ip" ] || die "public-domain TLS has no private CA to export"; export_ca ;;
  help) usage ;;
  *) usage; die "unknown action: $ACTION" ;;
esac
