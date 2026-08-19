import hmac
from typing import Optional, Tuple
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import get_db
from app.models.tenant import ApiKey, Tenant
from app.services.auth_service import authenticate_api_key, verify_access_token

security_bearer = HTTPBearer(auto_error=False)


async def get_current_tenant_and_key(
    request: Request,
    auth_cred: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_admin_secret: Optional[str] = Header(None, alias="X-Admin-Secret"),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    db: AsyncSession = Depends(get_db),
) -> Tuple[Tenant, ApiKey]:
    """
    Authenticates tenant via Bearer token or X-API-Key header.
    Also supports Admin workbench context for seamless UI testing.
    """
    raw_key = None
    if auth_cred and auth_cred.credentials:
        raw_key = auth_cred.credentials
    elif x_api_key:
        raw_key = x_api_key

    allowed_admin_keys = [settings.ADMIN_SECRET_KEY]

    # 1. Check if token or header represents Admin
    is_admin = False
    if x_admin_secret and settings.raw_admin_secret_auth_enabled:
        for k in allowed_admin_keys:
            if k:
                try:
                    if hmac.compare_digest(x_admin_secret.encode("utf-8"), k.encode("utf-8")):
                        is_admin = True
                        break
                except Exception:
                    if x_admin_secret == k:
                        is_admin = True
                        break

    if raw_key and not is_admin:
        # Check signed admin session token
        claims = verify_access_token(raw_key, expected_role="admin")
        if claims:
            is_admin = True
        else:
            # Legacy raw-secret authentication is intentionally disabled in production.
            if settings.raw_admin_secret_auth_enabled:
                for k in allowed_admin_keys:
                    if k:
                        try:
                            if hmac.compare_digest(raw_key.encode("utf-8"), k.encode("utf-8")):
                                is_admin = True
                                break
                        except Exception:
                            if raw_key == k:
                                is_admin = True
                                break

    # 2. If Admin access, resolve target tenant
    if is_admin:
        stmt = select(Tenant).options(selectinload(Tenant.api_keys)).where(Tenant.is_active.is_(True))
        if x_tenant_id:
            stmt = stmt.where(Tenant.id == x_tenant_id)
        stmt = stmt.order_by(Tenant.created_at.desc())
        res = await db.execute(stmt)
        tenant = res.scalars().first()
        if tenant:
            active_key = next((key for key in tenant.api_keys if key.is_active), None)
            if not active_key:
                active_key = ApiKey(
                    id=f"admin_virtual_key_{tenant.id}",
                    tenant_id=tenant.id,
                    name="Admin Console Virtual Key",
                    key_prefix="cg_admin",
                    key_hash="",
                    api_secret="admin-secret",
                )
            return tenant, active_key

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "No active tenant found for admin testing."},
        )

    # 3. If regular tenant authentication
    if raw_key:
        session_claims = verify_access_token(raw_key, expected_role="tenant")
        if session_claims:
            stmt = select(Tenant).options(selectinload(Tenant.api_keys)).where(
                Tenant.id == session_claims["sub"],
                Tenant.is_active.is_(True),
            )
            tenant = (await db.execute(stmt)).scalar_one_or_none()
            if tenant:
                active_key = next((key for key in tenant.api_keys if key.is_active), None)
                if active_key:
                    return tenant, active_key

        auth_result = await authenticate_api_key(db, raw_key)
        if auth_result:
            return auth_result

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": 40102, "message": "Invalid or inactive API Key / Session."},
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": 40101, "message": "Missing API Key. Provide via Bearer token or X-API-Key header."},
    )



async def verify_admin_access(
    request: Request,
    x_admin_secret: Optional[str] = Header(None, alias="X-Admin-Secret"),
) -> bool:
    """Verifies admin access key for management console and administrative APIs."""
    auth_header = request.headers.get("Authorization", "")
    secret_candidate = x_admin_secret
    if not secret_candidate and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        claims = verify_access_token(token, expected_role="admin")
        if claims:
            return True
        if settings.raw_admin_secret_auth_enabled:
            secret_candidate = token

    allowed_admin_keys = [settings.ADMIN_SECRET_KEY]
    if secret_candidate and settings.raw_admin_secret_auth_enabled:
        for k in allowed_admin_keys:
            if k:
                try:
                    if hmac.compare_digest(secret_candidate.encode("utf-8"), k.encode("utf-8")):
                        return True
                except Exception:
                    if secret_candidate == k:
                        return True

    # Also allow if debug mode is active and local request
    client_ip = request.client.host if request.client else "127.0.0.1"
    if settings.DEBUG and client_ip in {"127.0.0.1", "localhost", "::1"}:
        return True

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": 40301, "message": "Admin access forbidden. Invalid X-Admin-Secret."},
    )
