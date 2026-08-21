from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.tenant import ApiKey, Tenant
from app.schemas.tenant import ApiKeyResponse, TenantResponse
from app.api.deps import get_current_tenant_and_key
from app.services.auth_service import generate_api_key_and_secret

router = APIRouter()


@router.get(
    "/tenants/me",
    response_model=TenantResponse,
    summary="查询当前租户信息与账户余额",
)
async def get_my_tenant_profile(
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
):
    tenant, _ = tenant_info
    return tenant


@router.get(
    "/tenants/me/keys",
    response_model=List[ApiKeyResponse],
    summary="查询当前租户所有 API Keys 与 Secret",
)
async def get_my_api_keys(
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info
    stmt = select(ApiKey).where(ApiKey.tenant_id == tenant.id).order_by(ApiKey.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post(
    "/tenants/me/keys",
    response_model=ApiKeyResponse,
    summary="当前租户自助生成新的 API Key 凭证",
)
async def generate_my_api_key(
    key_name: str = Query("自助生成 API Key", description="密钥名称/用途"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
    api_key = ApiKey(
        tenant_id=tenant.id,
        name=key_name,
        key_prefix=prefix,
        key_hash=key_hash,
        raw_key=raw_key,
        api_secret=secret,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    resp = ApiKeyResponse.model_validate(api_key)
    resp.raw_api_key = raw_key
    resp.api_secret = secret
    return resp

