import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.models.tenant import ApiKey, Tenant

PASSWORD_SCHEME = "pbkdf2_sha256"  # noqa: S105 - algorithm identifier, not a credential
PASSWORD_ITERATIONS = 310_000
SESSION_TOKEN_PREFIX = "cgs_"  # noqa: S105 - public token format marker


def generate_api_key_and_secret() -> Tuple[str, str, str, str]:
    """
    Generates a new API Key and Secret.
    Returns: (raw_key, key_prefix, key_hash, api_secret)
    """
    prefix = f"cg_{secrets.token_hex(4)}"
    secret_token = secrets.token_urlsafe(32)
    raw_key = f"{prefix}_{secret_token}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    api_secret = secrets.token_hex(24)
    return raw_key, prefix, key_hash, api_secret


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Hash a password with a unique salt and a deliberately expensive KDF."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
        )
    )


def _decode_b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify current hashes and the legacy SHA-256 format used by older deployments."""
    if not stored_hash:
        return False
    try:
        if stored_hash.startswith(f"{PASSWORD_SCHEME}$"):
            _, iterations, salt_value, digest_value = stored_hash.split("$", 3)
            expected = _decode_b64(digest_value)
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                _decode_b64(salt_value),
                int(iterations),
            )
            return hmac.compare_digest(expected, actual)

        legacy = hashlib.sha256(f"cargo_pwd_salt_{password}".encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored_hash, legacy)
    except (TypeError, ValueError):
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    if not stored_hash.startswith(f"{PASSWORD_SCHEME}$"):
        return True
    try:
        return int(stored_hash.split("$", 2)[1]) < PASSWORD_ITERATIONS
    except (IndexError, ValueError):
        return True


def create_access_token(subject: str, role: str = "tenant", expires_in: Optional[int] = None) -> str:
    """Create a compact, signed, expiring session token without exposing API/admin secrets."""
    secret = settings.session_secret
    if not secret:
        raise RuntimeError("SESSION_SECRET_KEY or ADMIN_SECRET_KEY must be configured")
    now = int(time.time())
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + (expires_in or settings.AUTH_TOKEN_TTL_SECONDS),
        "nonce": secrets.token_hex(8),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{SESSION_TOKEN_PREFIX}{encoded}.{encoded_signature}"


def verify_access_token(token: str, expected_role: Optional[str] = None) -> Optional[Dict[str, Any]]:
    secret = settings.session_secret
    if not secret or not token or not token.startswith(SESSION_TOKEN_PREFIX):
        return None
    try:
        encoded, encoded_signature = token[len(SESSION_TOKEN_PREFIX):].split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _decode_b64(encoded_signature)):
            return None
        payload = json.loads(_decode_b64(encoded))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if expected_role and payload.get("role") != expected_role:
            return None
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None



async def authenticate_api_key(db: AsyncSession, raw_key: str) -> Optional[Tuple[Tenant, ApiKey]]:
    """
    Authenticates a raw API key.
    Returns: (Tenant, ApiKey) if valid and active, else None.
    """
    if not raw_key or not raw_key.strip():
        return None

    key_hash = hash_api_key(raw_key.strip())
    stmt = (
        select(ApiKey, Tenant)
        .join(Tenant, ApiKey.tenant_id == Tenant.id)
        .where(ApiKey.key_hash == key_hash, ApiKey.is_active == True, Tenant.is_active == True)
    )
    result = await db.execute(stmt)
    row = result.first()
    if not row:
        return None

    api_key, tenant = row
    return tenant, api_key
