from decimal import Decimal
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.tenant import ApiKey, Tenant
from app.schemas.tenant import (
    ApiKeyResponse,
    RechargeRequest,
    TenantCreate,
    TenantResponse,
    TenantUpdate,
    UpdateUnitPriceRequest,
)

from app.api.deps import verify_admin_access
from app.services.auth_service import generate_api_key_and_secret
from app.services.billing_service import BillingService

router = APIRouter(prefix="/admin/tenants", dependencies=[Depends(verify_admin_access)])


@router.get("", response_model=List[TenantResponse], summary="管理员查询所有租户列表")
async def list_all_tenants(
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Tenant).order_by(Tenant.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post("", response_model=TenantResponse, summary="管理员创建新租户并生成初始 API Key")
async def create_tenant(
    data: TenantCreate,
    db: AsyncSession = Depends(get_db),
):
    if data.contact_email:
        duplicate = await db.execute(
            select(Tenant.id)
            .where(func.lower(Tenant.contact_email) == data.contact_email.lower())
            .limit(1)
        )
        if duplicate.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": 40901, "message": "Contact email is already registered"},
            )

    tenant = Tenant(
        name=data.name,
        contact_email=data.contact_email.strip().lower() if data.contact_email else None,
        contact_phone=data.contact_phone,
        unit_price=data.unit_price,
        max_concurrency=data.max_concurrency,
        balance=Decimal("0.0000"),
    )
    db.add(tenant)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": 40901, "message": "Contact email is already registered"},
        )
    await db.refresh(tenant)

    # Generate initial API Key
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
    api_key = ApiKey(
        tenant_id=tenant.id,
        name="Default Live Key",
        key_prefix=prefix,
        key_hash=key_hash,
        api_secret=secret,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(tenant)

    # Initial recharge if specified
    if data.initial_balance > 0:
        await BillingService.recharge_balance(
            db, tenant.id, data.initial_balance, description="开户初始充值", operator="ADMIN"
        )
        await db.refresh(tenant)

    # Attach raw key for one-time display
    resp = TenantResponse.model_validate(tenant)
    if resp.api_keys:
        resp.api_keys[0].raw_api_key = raw_key
        resp.api_keys[0].api_secret = secret

    return resp


@router.put("/{tenant_id}", response_model=TenantResponse, summary="更新租户配置信息 (单价、并发上限、名称、电话、启用状态)")
async def update_tenant(
    tenant_id: str,
    data: TenantUpdate,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    res = await db.execute(stmt)
    tenant = res.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if data.name is not None:
        tenant.name = data.name.strip()
    if data.contact_phone is not None:
        tenant.contact_phone = data.contact_phone.strip()
    if data.unit_price is not None:
        tenant.unit_price = data.unit_price
    if data.max_concurrency is not None:
        tenant.max_concurrency = data.max_concurrency
    if data.is_active is not None:
        tenant.is_active = data.is_active

    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.put("/{tenant_id}/status", summary="管理员审核/启停用租户账号")
async def update_tenant_status(
    tenant_id: str,
    is_active: bool = Query(..., description="启用(True) 或 禁用(False)"),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    res = await db.execute(stmt)
    tenant = res.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant.is_active = is_active
    await db.commit()
    await db.refresh(tenant)
    status_text = "审核通过并已启用" if is_active else "已设为待审核"
    return {
        "code": 0,
        "message": f"租户 {tenant.name} {status_text}",
        "data": {
            "tenant_id": tenant.id,
            "is_active": tenant.is_active,
        },
    }


@router.post("/{tenant_id}/recharge", summary="管理员为租户充值账户余额")
async def recharge_tenant_direct(
    tenant_id: str,
    data: RechargeRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        tx = await BillingService.recharge_balance(
            db=db,
            tenant_id=tenant_id,
            amount=data.amount,
            description=data.description or "管理员人工充值",
            operator=data.operator or "ADMIN",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": 42202, "message": str(exc)},
        ) from exc
    if not tx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "Tenant not found"},
        )
    return tx


@router.put("/{tenant_id}/unit-price", summary="更新租户单次调用单价")
async def update_tenant_unit_price(
    tenant_id: str,
    data: UpdateUnitPriceRequest,
    db: AsyncSession = Depends(get_db),
):
    success = await BillingService.update_unit_price(db, tenant_id, data.unit_price)
    if not success:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"code": 0, "message": f"Unit price updated to {data.unit_price} RMB"}



@router.get("/{tenant_id}/keys", response_model=List[ApiKeyResponse], summary="查询租户所有 API Keys 与 Secret")
async def list_tenant_api_keys(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post("/{tenant_id}/keys", response_model=ApiKeyResponse, summary="为租户生成新的 API Key")
async def create_tenant_api_key(
    tenant_id: str,
    key_name: str = Query("New API Key", description="密钥名称/用途说明"),
    db: AsyncSession = Depends(get_db),
):

    stmt = select(Tenant).where(Tenant.id == tenant_id)
    res = await db.execute(stmt)
    tenant = res.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
    api_key = ApiKey(
        tenant_id=tenant.id,
        name=key_name,
        key_prefix=prefix,
        key_hash=key_hash,
        api_secret=secret,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    resp = ApiKeyResponse.model_validate(api_key)
    resp.raw_api_key = raw_key
    resp.api_secret = secret
    return resp


@router.delete("/keys/{key_id}", summary="吊销/删除 API Key")
async def revoke_api_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ApiKey).where(ApiKey.id == key_id)
    res = await db.execute(stmt)
    api_key = res.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key not found")

    await db.delete(api_key)
    await db.commit()
    return {"code": 0, "message": "API Key revoked successfully"}
