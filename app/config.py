from pathlib import Path
from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.core.limits import (
    DEFAULT_TENANT_CONCURRENCY as DEFAULT_TENANT_CONCURRENCY_LIMIT,
    MAX_TENANT_CONCURRENCY,
    MAX_WORKER_CONCURRENCY,
    MIN_TENANT_CONCURRENCY,
    MIN_WORKER_CONCURRENCY,
)

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Environment & Server
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Database
    DATABASE_URL: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'cargo_service.db'}"
    DATABASE_URL_FILE: str = ""

    # Durable task queue
    TASK_QUEUE_MODE: str = "celery"
    CELERY_BROKER_URL: str = "redis://127.0.0.1:6379/0"
    CELERY_BROKER_URL_FILE: str = ""
    CELERY_RESULT_BACKEND: str = "redis://127.0.0.1:6379/1"
    CELERY_RESULT_BACKEND_FILE: str = ""
    REDIS_SENTINEL_URLS: str = ""
    REDIS_SENTINEL_MASTER_NAME: str = "mymaster"
    REDIS_PASSWORD: str = ""
    REDIS_PASSWORD_FILE: str = ""
    CELERY_TASK_ALWAYS_EAGER: bool = False
    CELERY_QUEUE_NAME: str = "cargo-extraction"
    CELERY_WEBHOOK_QUEUE_NAME: str = "cargo-webhooks"
    TASK_LEASE_SECONDS: int = Field(default=600, ge=60, le=3600)
    TASK_RECOVERY_INTERVAL_SECONDS: int = Field(default=30, ge=10, le=300)
    TASK_DISPATCH_STALE_SECONDS: int = Field(default=120, ge=30, le=3600)
    TASK_RECOVERY_BATCH_SIZE: int = Field(default=500, ge=1, le=5000)
    BEAT_LOCK_TTL_SECONDS: int = Field(default=25, ge=10, le=300)
    MAX_TENANT_PENDING_TASKS: int = Field(default=1000, ge=1, le=100000)
    MAX_GLOBAL_PENDING_TASKS: int = Field(default=10000, ge=1, le=1000000)

    # LLM Settings (SenseTime Default)
    LLM_BASE_URL: str = "https://api.senseaudio.cn/v1"
    LLM_API_KEY: str = ""
    LLM_API_KEY_FILE: str = ""
    LLM_MODEL: str = "senseaudio-s2"
    LLM_FALLBACK_MODEL: str = "deepseek-v4-flash-0731"
    LLM_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0)
    LLM_TIMEOUT_SECONDS: int = Field(default=60, ge=5, le=600)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0, le=10)

    # Multimodal Vision Model Settings (Shares LLM_BASE_URL & LLM_API_KEY)
    VISION_LLM_ENABLED: bool = True
    VISION_LLM_MODEL: str = "qwen3.8-27b"
    VISION_LLM_TIMEOUT_SECONDS: int = Field(default=30, ge=5, le=300)
    VISION_MAX_IMAGES_PER_TASK: int = Field(default=5, ge=1, le=20)

    # Skill V3 Assets Location
    SKILL_V3_PATH: str = str(BASE_DIR.parent / "cargo-mail-extraction-skill-v3")

    # Tenant & Billing
    DEFAULT_UNIT_PRICE: float = Field(default=0.50, ge=0.01, le=10000.0)
    DEFAULT_TENANT_CONCURRENCY: int = Field(
        default=DEFAULT_TENANT_CONCURRENCY_LIMIT,
        ge=MIN_TENANT_CONCURRENCY,
        le=MAX_TENANT_CONCURRENCY,
    )

    # Worker Queue
    WORKER_CONCURRENCY: int = Field(
        default=30,
        ge=MIN_WORKER_CONCURRENCY,
        le=MAX_WORKER_CONCURRENCY,
    )
    TASK_TIMEOUT_SECONDS: int = Field(default=300, ge=30, le=3600)

    # Data Retention & Storage
    DATA_RETENTION_DAYS: int = Field(default=90, ge=1, le=3650)
    UPLOAD_DIR: str = str(BASE_DIR / "data" / "uploads")
    MAX_UPLOAD_FILES: int = Field(default=10, ge=1, le=50)
    MAX_UPLOAD_FILE_SIZE: int = Field(default=50 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    MAX_LEGACY_DOC_FILE_SIZE: int = Field(default=20 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    MAX_UPLOAD_TOTAL_SIZE: int = Field(default=100 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024)

    # Webhook
    WEBHOOK_MAX_RETRIES: int = Field(default=3, ge=1, le=10)
    WEBHOOK_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=60)

    # Authentication & browser access
    ADMIN_SECRET_KEY: str = ""
    ADMIN_SECRET_KEY_FILE: str = ""
    SESSION_SECRET_KEY: str = ""
    SESSION_SECRET_KEY_FILE: str = ""
    AUTH_TOKEN_TTL_SECONDS: int = Field(default=8 * 60 * 60, ge=300, le=24 * 60 * 60)
    CORS_ALLOWED_ORIGINS: str = "http://127.0.0.1:8000,http://localhost:8000"
    ALLOWED_HOSTS: str = "127.0.0.1,localhost"
    TRUST_PROXY_HEADERS: bool = False
    ALLOW_LEGACY_ADMIN_SECRET_AUTH: bool = False
    AUTH_RATE_LIMIT_ENABLED: bool = False
    TENANT_LOGIN_RATE_LIMIT_ATTEMPTS: int = Field(default=10, ge=1, le=1000)
    TENANT_LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=300, ge=10, le=86400)
    ADMIN_LOGIN_RATE_LIMIT_ATTEMPTS: int = Field(default=5, ge=1, le=100)
    ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=300, ge=10, le=86400)
    REGISTER_RATE_LIMIT_ATTEMPTS: int = Field(default=10, ge=1, le=500)
    REGISTER_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=3600, ge=60, le=86400)
    SEED_DEMO_TENANT: bool = False
    METRICS_ENABLED: bool = False

    @model_validator(mode="after")
    def load_file_secrets(self) -> "Settings":
        """Load secrets mounted by Docker Compose without copying them into env."""
        secret_files = {
            "DATABASE_URL": self.DATABASE_URL_FILE,
            "CELERY_BROKER_URL": self.CELERY_BROKER_URL_FILE,
            "CELERY_RESULT_BACKEND": self.CELERY_RESULT_BACKEND_FILE,
            "REDIS_PASSWORD": self.REDIS_PASSWORD_FILE,
            "LLM_API_KEY": self.LLM_API_KEY_FILE,
            "ADMIN_SECRET_KEY": self.ADMIN_SECRET_KEY_FILE,
            "SESSION_SECRET_KEY": self.SESSION_SECRET_KEY_FILE,
        }
        for field_name, file_name in secret_files.items():
            if not file_name:
                continue
            secret_path = Path(file_name)
            try:
                if secret_path.stat().st_size > 64 * 1024:
                    raise ValueError(f"{field_name}_FILE is unexpectedly large")
                secret_value = secret_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"Unable to read {field_name}_FILE") from exc
            if not secret_value:
                raise ValueError(f"{field_name}_FILE must not be empty")
            setattr(self, field_name, secret_value)
        return self

    @model_validator(mode="after")
    def validate_upload_limits(self) -> "Settings":
        if self.MAX_LEGACY_DOC_FILE_SIZE > self.MAX_UPLOAD_FILE_SIZE:
            raise ValueError("MAX_LEGACY_DOC_FILE_SIZE must not exceed MAX_UPLOAD_FILE_SIZE")
        if self.MAX_UPLOAD_FILE_SIZE > self.MAX_UPLOAD_TOTAL_SIZE:
            raise ValueError("MAX_UPLOAD_FILE_SIZE must not exceed MAX_UPLOAD_TOTAL_SIZE")
        return self

    @property
    def session_secret(self) -> str:
        """Return the dedicated session key, falling back to the admin key for upgrades."""
        return self.SESSION_SECRET_KEY.strip() or self.ADMIN_SECRET_KEY.strip() or "cargo-plus-session-secret-2026"


    @property
    def cors_allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def allowed_hosts(self) -> list[str]:
        return [host.strip() for host in self.ALLOWED_HOSTS.split(",") if host.strip()]

    @property
    def raw_admin_secret_auth_enabled(self) -> bool:
        """Keep the legacy header usable for local upgrades, never by default in production."""
        return self.ENVIRONMENT.lower() != "production" or self.ALLOW_LEGACY_ADMIN_SECRET_AUTH

    def validate_security_settings(self) -> None:
        """Fail closed in production when authentication secrets are absent or known defaults."""
        insecure_values = {"", "cargo-plus-admin-secret-2026", "change-me", "changeme"}
        if self.ENVIRONMENT.lower() == "production":
            if self.ADMIN_SECRET_KEY.strip() in insecure_values:
                raise RuntimeError("ADMIN_SECRET_KEY must be set to a strong non-default value in production")
            if self.session_secret in insecure_values:
                raise RuntimeError("SESSION_SECRET_KEY must be set to a strong non-default value in production")
            if len(self.ADMIN_SECRET_KEY.strip()) < 32 or len(self.session_secret) < 32:
                raise RuntimeError("Production authentication secrets must contain at least 32 characters")
            if self.ADMIN_SECRET_KEY.strip() == self.session_secret:
                raise RuntimeError("ADMIN_SECRET_KEY and SESSION_SECRET_KEY must be different")
            if self.LLM_API_KEY.strip().lower() in insecure_values:
                raise RuntimeError("LLM_API_KEY must be configured in production")
            if not self.DATABASE_URL.startswith("postgresql+asyncpg://"):
                raise RuntimeError("Production DATABASE_URL must use PostgreSQL with asyncpg")
            if not self.CELERY_BROKER_URL.startswith(("redis://", "rediss://", "sentinel://")):
                raise RuntimeError("Production CELERY_BROKER_URL must use Redis or Redis Sentinel")
            if not self.CELERY_RESULT_BACKEND.startswith(("redis://", "rediss://", "sentinel://")):
                raise RuntimeError(
                    "Production CELERY_RESULT_BACKEND must use Redis or Redis Sentinel"
                )
            if "*" in self.cors_allowed_origins:
                raise RuntimeError("Wildcard CORS origins are forbidden in production")
            if not self.cors_allowed_origins or any(
                not (origin.startswith("https://") or origin.startswith("http://")) for origin in self.cors_allowed_origins
            ):
                raise RuntimeError("Production CORS_ALLOWED_ORIGINS must contain explicit http:// or https:// origins")
            if not self.allowed_hosts or "*" in self.allowed_hosts:
                raise RuntimeError("Production ALLOWED_HOSTS must be explicit and must not contain a wildcard")
            if not self.LLM_BASE_URL.startswith("https://"):
                raise RuntimeError("Production LLM_BASE_URL must use HTTPS")
            if self.TASK_QUEUE_MODE != "celery":
                raise RuntimeError("Production TASK_QUEUE_MODE must be 'celery'")
            if not self.AUTH_RATE_LIMIT_ENABLED:
                raise RuntimeError("AUTH_RATE_LIMIT_ENABLED must be enabled in production")
        if self.TASK_QUEUE_MODE not in {"celery", "local"}:
            raise RuntimeError("TASK_QUEUE_MODE must be either 'celery' or 'local'")

    @property
    def skill_path(self) -> Path:
        candidates = [
            Path(self.SKILL_V3_PATH),
            BASE_DIR / self.SKILL_V3_PATH,
            BASE_DIR / "skill_v3",
            BASE_DIR / "data" / "skill_v3",
            BASE_DIR.parent / "cargo-mail-extraction-skill-v3",
        ]
        for c in candidates:
            resolved = c.resolve() if not c.is_absolute() else c
            if resolved.exists() and (resolved / "prompts").exists():
                return resolved
        fallback = (BASE_DIR / "skill_v3").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    @property
    def uploads_path(self) -> Path:
        p = Path(self.UPLOAD_DIR)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()

# Ensure local data directories exist
data_dir = BASE_DIR / "data"
data_dir.mkdir(parents=True, exist_ok=True)
settings.uploads_path.mkdir(parents=True, exist_ok=True)
