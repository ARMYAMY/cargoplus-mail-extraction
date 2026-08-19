#!/bin/sh
set -eu
umask 077

backup_dir="${BACKUP_DIR:-/backups}"
if [ "$backup_dir" != "/backups" ]; then
  echo "BACKUP_DIR must be the dedicated /backups mount" >&2
  exit 1
fi
password_file="${POSTGRES_PASSWORD_FILE:-/run/secrets/restore_drill_password}"
if [ ! -r "$password_file" ]; then
  echo "Restore drill password secret is not readable" >&2
  exit 1
fi
export PGPASSWORD="$(tr -d '\r\n' < "$password_file")"

latest="$(find "$backup_dir" -maxdepth 1 -type f -name 'cargoplus-*.dump' | sort | tail -n 1)"
if [ -z "$latest" ] || [ ! -f "$latest.sha256" ]; then
  echo "No checksummed CargoPlus backup is available" >&2
  exit 1
fi

cd "$backup_dir"
sha256sum -c "$(basename "$latest").sha256"
pg_restore --exit-on-error --no-owner --no-privileges \
  --host="${PGHOST:-restore-db}" --port="${PGPORT:-5432}" \
  --username="${PGUSER:-cargo_restore}" --dbname="${PGDATABASE:-cargo_restore}" \
  "$latest"

psql -X --set=ON_ERROR_STOP=1 \
  --host="${PGHOST:-restore-db}" --port="${PGPORT:-5432}" \
  --username="${PGUSER:-cargo_restore}" --dbname="${PGDATABASE:-cargo_restore}" <<'SQL'
SELECT count(*) AS tenants FROM tenants;
SELECT count(*) AS tasks FROM email_tasks;
SQL

invariant_violations="$(psql -XAt --set=ON_ERROR_STOP=1 \
  --host="${PGHOST:-restore-db}" --port="${PGPORT:-5432}" \
  --username="${PGUSER:-cargo_restore}" --dbname="${PGDATABASE:-cargo_restore}" \
  --command="SELECT count(*) FROM tenants WHERE balance < 0 OR reserved_balance < 0 OR reserved_balance > balance OR unit_price < 0.01 OR unit_price > 100 OR max_concurrency < 1 OR max_concurrency > 30")"
duplicate_task_charges="$(psql -XAt --set=ON_ERROR_STOP=1 \
  --host="${PGHOST:-restore-db}" --port="${PGPORT:-5432}" \
  --username="${PGUSER:-cargo_restore}" --dbname="${PGDATABASE:-cargo_restore}" \
  --command="SELECT count(*) FROM (SELECT task_id FROM billing_transactions WHERE task_id IS NOT NULL AND type = 'DEDUCTION' GROUP BY task_id HAVING count(*) > 1) AS duplicate_rows")"
if [ "$invariant_violations" != "0" ] || [ "$duplicate_task_charges" != "0" ]; then
  echo "Restore validation failed: invariant_violations=$invariant_violations duplicate_task_charges=$duplicate_task_charges" >&2
  exit 1
fi

date +%s > "$backup_dir/.last_restore_drill_success"
chmod 0644 "$backup_dir/.last_restore_drill_success"
echo "Restore drill completed successfully using $(basename "$latest")"
