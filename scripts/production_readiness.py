from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse


PLACEHOLDERS = {"", "change-me", "changeme", "example", "your-production-model"}
SECRET_PATH_KEYS = (
    "DATABASE_URL_SECRET_FILE",
    "POSTGRES_BACKUP_URL_SECRET_FILE",
    "CELERY_BROKER_URL_SECRET_FILE",
    "CELERY_RESULT_BACKEND_SECRET_FILE",
    "REDIS_PASSWORD_SECRET_FILE",
    "LLM_API_KEY_SECRET_FILE",
    "ADMIN_SECRET_FILE",
    "SESSION_SECRET_FILE",
    "ALERT_WEBHOOK_URL_SECRET_FILE",
    "GRAFANA_ADMIN_PASSWORD_SECRET_FILE",
    "RESTORE_DRILL_PASSWORD_SECRET_FILE",
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_secret(root: Path, env: dict[str, str], key: str, errors: list[str]) -> str:
    value = env.get(key, "")
    if not value:
        errors.append(f"{key} is missing")
        return ""
    path = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if path.suffix == ".example":
        errors.append(f"{key} still points to an example file")
        return ""
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        errors.append(f"{key} is not readable: {path}")
        return ""
    if secret.lower() in PLACEHOLDERS or "example.internal" in secret or "example.invalid" in secret:
        errors.append(f"{key} contains a placeholder value")
        return ""
    return secret


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate CargoPlus production deployment inputs")
    parser.add_argument("--env-file", type=Path, default=Path("deploy/.env.production"))
    parser.add_argument("--skip-compose", action="store_true")
    args = parser.parse_args()

    env_path = args.env_file.resolve()
    if not env_path.is_file():
        raise SystemExit(f"Production env file is missing: {env_path}")
    root = Path(__file__).resolve().parents[1]
    env = read_env(env_path)
    errors: list[str] = []

    app_domain = env.get("APP_DOMAIN", "")
    monitor_domain = env.get("MONITOR_DOMAIN", "")
    for key, value in (("APP_DOMAIN", app_domain), ("MONITOR_DOMAIN", monitor_domain)):
        if not value or "example." in value or "://" in value:
            errors.append(f"{key} must be a real DNS hostname without a URL scheme")
    if app_domain and app_domain == monitor_domain:
        errors.append("APP_DOMAIN and MONITOR_DOMAIN must be different")
    if env.get("CORS_ALLOWED_ORIGINS") != f"https://{app_domain}":
        errors.append("CORS_ALLOWED_ORIGINS must exactly match https://APP_DOMAIN")
    if not env.get("ACME_EMAIL") or "@example." in env.get("ACME_EMAIL", ""):
        errors.append("ACME_EMAIL must be a real operations address")
    if not env.get("LLM_BASE_URL", "").startswith("https://"):
        errors.append("LLM_BASE_URL must use HTTPS")
    image = env.get("CARGOPLUS_IMAGE", "")
    digest_match = re.search(r"@sha256:([0-9a-f]{64})$", image)
    if not digest_match or set(digest_match.group(1)) == {"0"}:
        errors.append("CARGOPLUS_IMAGE must use a real immutable sha256 digest")
    if env.get("LLM_MODEL", "").lower() in PLACEHOLDERS:
        errors.append("LLM_MODEL must name the approved production model")
    backup_dir_value = env.get("POSTGRES_BACKUP_DIR", "")
    backup_dir = Path(backup_dir_value) if backup_dir_value else None
    if not backup_dir or not backup_dir.is_absolute() or str(backup_dir) in {"/", "\\"}:
        errors.append("POSTGRES_BACKUP_DIR must be a dedicated absolute directory")
    elif not backup_dir.is_dir():
        errors.append("POSTGRES_BACKUP_DIR must exist before deployment")

    secrets = {key: read_secret(root, env, key, errors) for key in SECRET_PATH_KEYS}
    database_url = secrets.get("DATABASE_URL_SECRET_FILE", "")
    if database_url and not database_url.startswith("postgresql+asyncpg://"):
        errors.append("DATABASE_URL secret must use postgresql+asyncpg://")
    if database_url and "ssl=" not in database_url:
        errors.append("DATABASE_URL secret must explicitly require TLS")
    backup_url = secrets.get("POSTGRES_BACKUP_URL_SECRET_FILE", "")
    if backup_url and not backup_url.startswith(("postgresql://", "postgres://")):
        errors.append("PostgreSQL backup URL must use a libpq-compatible scheme")
    if backup_url and "sslmode=require" not in backup_url:
        errors.append("PostgreSQL backup URL must include sslmode=require")

    broker_url = secrets.get("CELERY_BROKER_URL_SECRET_FILE", "")
    if broker_url and not broker_url.startswith(("rediss://", "sentinel://")):
        errors.append("Production Celery broker must use rediss:// or sentinel://")
    result_backend_url = secrets.get("CELERY_RESULT_BACKEND_SECRET_FILE", "")
    if result_backend_url and not result_backend_url.startswith(("rediss://", "sentinel://")):
        errors.append("Production Celery result backend must use rediss:// or sentinel://")
    sentinel_urls = [item.strip() for item in env.get("REDIS_SENTINEL_URLS", "").split(",") if item.strip()]
    if broker_url.startswith("sentinel://") and len(sentinel_urls) < 3:
        errors.append("Sentinel mode requires at least three REDIS_SENTINEL_URLS entries")

    admin_secret = secrets.get("ADMIN_SECRET_FILE", "")
    session_secret = secrets.get("SESSION_SECRET_FILE", "")
    if admin_secret and len(admin_secret) < 32:
        errors.append("Admin secret must contain at least 32 characters")
    if session_secret and len(session_secret) < 32:
        errors.append("Session secret must contain at least 32 characters")
    if admin_secret and admin_secret == session_secret:
        errors.append("Admin and session secrets must be different")
    alert_url = secrets.get("ALERT_WEBHOOK_URL_SECRET_FILE", "")
    if alert_url and urlparse(alert_url).scheme != "https":
        errors.append("Alert webhook URL must use HTTPS")

    if not args.skip_compose:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(env_path),
                "-f",
                str(root / "docker-compose.production.yml"),
                "config",
                "--quiet",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            errors.append(f"Docker Compose validation failed: {result.stderr.strip()}")

    if errors:
        print("Production readiness: FAILED")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    print("Production readiness: PASSED")


if __name__ == "__main__":
    main()
