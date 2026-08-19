#!/bin/sh
set -eu
umask 077

secret_file="${POSTGRES_BACKUP_URL_FILE:-/run/secrets/postgres_backup_url}"
if [ ! -r "$secret_file" ]; then
  echo "PostgreSQL backup URL secret is not readable" >&2
  exit 1
fi

database_url="$(tr -d '\r\n' < "$secret_file")"
case "$database_url" in
  postgresql://*|postgres://*) ;;
  *) echo "Backup URL must start with postgresql:// or postgres://" >&2; exit 1 ;;
esac

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
  partial="$backup_dir/.cargoplus-$timestamp.dump.partial"
  target="$backup_dir/cargoplus-$timestamp.dump"
  checksum="$target.sha256"
  trap 'rm -f "$partial"' EXIT INT TERM
  pg_dump --format=custom --compress=9 --no-owner --no-privileges \
    --file="$partial" "$database_url"
  pg_restore --list "$partial" >/dev/null
  mv "$partial" "$target"
  sha256sum "$target" > "$checksum"
  date +%s > "$backup_dir/.last_backup_success"
  chmod 0644 "$backup_dir/.last_backup_success"
  find "$backup_dir" -maxdepth 1 -type f \
    \( -name 'cargoplus-*.dump' -o -name 'cargoplus-*.dump.sha256' \) \
    -mtime "+$retention_days" -delete
  trap - EXIT INT TERM
  echo "Verified PostgreSQL backup created: $target"
}

run_backup
if [ "${BACKUP_RUN_ONCE:-false}" = "true" ]; then
  exit 0
fi

while sleep "$interval_seconds"; do
  run_backup
done
