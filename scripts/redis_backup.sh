#!/bin/sh
set -eu
umask 077

secret_file="${REDIS_PASSWORD_FILE:-/run/secrets/redis_password}"
if [ ! -r "$secret_file" ]; then
  echo "Redis password secret is not readable" >&2
  exit 1
fi
export REDISCLI_AUTH="$(tr -d '\r\n' < "$secret_file")"

backup_dir="${BACKUP_DIR:-/backups}"
if [ "$backup_dir" != "/backups" ]; then
  echo "BACKUP_DIR must be the dedicated /backups mount" >&2
  exit 1
fi
retention_days="${BACKUP_RETENTION_DAYS:-14}"
interval_seconds="${BACKUP_INTERVAL_SECONDS:-86400}"
case "$retention_days" in *[!0-9]*|'') exit 1 ;; esac
case "$interval_seconds" in *[!0-9]*|'') exit 1 ;; esac
mkdir -p "$backup_dir"

run_backup() {
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  partial="$backup_dir/.cargoplus-redis-$timestamp.rdb.partial"
  target="$backup_dir/cargoplus-redis-$timestamp.rdb"
  checksum="$target.sha256"
  trap 'rm -f "$partial"' EXIT INT TERM
  redis-cli -h "${REDIS_HOST:-redis}" -p "${REDIS_PORT:-6379}" --rdb "$partial"
  redis-check-rdb "$partial" >/dev/null
  mv "$partial" "$target"
  sha256sum "$target" > "$checksum"
  date +%s > "$backup_dir/.last_redis_backup_success"
  chmod 0644 "$backup_dir/.last_redis_backup_success"
  find "$backup_dir" -maxdepth 1 -type f \
    \( -name 'cargoplus-redis-*.rdb' -o -name 'cargoplus-redis-*.rdb.sha256' \) \
    -mtime "+$retention_days" -delete
  trap - EXIT INT TERM
  echo "Verified Redis RDB backup created: $target"
}

run_backup
if [ "${BACKUP_RUN_ONCE:-false}" = "true" ]; then
  exit 0
fi

while sleep "$interval_seconds"; do
  run_backup
done
