from decimal import Decimal
import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.core.limits import DEFAULT_TENANT_CONCURRENCY
from app.database import get_db
from app.models.tenant import ApiKey, Tenant
from app.services.auth_service import (
    authenticate_api_key,
    create_access_token,
    generate_api_key_and_secret,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.services.billing_service import BillingService

router = APIRouter(prefix="/auth", tags=["Authentication & Registration"])



COMMON_WEAK_PASSWORDS = {
    "1234567890", "0123456789", "abcdefghij", "password123", "password1234",
    "admin123456", "root1234567", "qwertyuiop", "1111111111", "0000000000",
}


def is_sequential_chars(s: str) -> bool:
    if len(s) < 4:
        return False
    forward = all(ord(s[i + 1]) - ord(s[i]) == 1 for i in range(len(s) - 1))
    backward = all(ord(s[i]) - ord(s[i + 1]) == 1 for i in range(len(s) - 1))
    return forward or backward


class TenantRegisterRequest(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=128, description="货代企业名称")
    contact_email: str = Field(..., min_length=5, max_length=128, description="联系人邮箱")
    contact_phone: Optional[str] = Field(None, max_length=32, description="联系电话")
    password: str = Field(..., min_length=10, max_length=128, description="登录密码")

    @field_validator("password")
    @classmethod
    def validate_strong_password(cls, value: str) -> str:
        if len(value) < 10:
            raise ValueError("密码长度至少须为 10 位")
        if not re.search(r"[A-Za-z]", value) or not re.search(r"[0-9]", value):
            raise ValueError("密码强度不足：须同时包含字母与数字")
        if len(set(value)) < 4:
            raise ValueError("密码过于简单：不能包含过多重复字符")
        if value.lower() in COMMON_WEAK_PASSWORDS or is_sequential_chars(value.lower()):
            raise ValueError("密码过于简单：禁止使用常用弱密码或连续递增字符")
        return value

    @field_validator("contact_phone")
    @classmethod
    def validate_contact_phone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped if stripped else None

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("contact_email is not a valid email address")
        return normalized



class TenantLoginRequest(BaseModel):
    account: str = Field(..., min_length=3, max_length=256, description="邮箱地址 或 API Key (cg_live_...)")
    password: Optional[str] = Field(None, max_length=128, description="登录密码 (使用 API Key 登录时可免密)")


class AdminLoginRequest(BaseModel):
    username: str = Field(default="admin", min_length=1, max_length=64, description="管理员用户名")
    password: str = Field(..., min_length=1, max_length=256, description="管理员密钥 / 密码")


@router.post("/register", summary="货代客户自助在线注册开户")
async def register_tenant(
    req: TenantRegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    # Check if company name is already registered
    normalized_company = req.company_name.strip()
    check_name_stmt = select(Tenant.id).where(func.lower(Tenant.name) == normalized_company.lower()).limit(1)
    res_name = await db.execute(check_name_stmt)
    if res_name.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": 40002,
                "message": f"企业名称「{req.company_name}」已被注册。若为同集团/分公司，请添加部门后缀（例如：{req.company_name}-深圳分部），或联系企业管理员加入。",
            },
        )

    # Check if email is already registered
    normalized_email = req.contact_email.strip().lower()
    check_stmt = select(Tenant.id).where(func.lower(Tenant.contact_email) == normalized_email).limit(1)
    res = await db.execute(check_stmt)
    if res.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40001, "message": f"邮箱 {req.contact_email} 已被注册，请直接登录"},
        )

    # 1. Create Tenant (Default is_active=False pending admin review)
    tenant = Tenant(
        name=req.company_name,
        contact_email=normalized_email,
        contact_phone=req.contact_phone,
        password_hash=hash_password(req.password),
        unit_price=Decimal("0.5000"),
        max_concurrency=DEFAULT_TENANT_CONCURRENCY,
        balance=Decimal("0.0000"),
        is_active=False,  # 默认待管理员审核
    )
    db.add(tenant)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": 40901, "message": "该邮箱已被注册"},
        )
    await db.refresh(tenant)

    # 2. Generate API Key & Secret
    raw_key, prefix, key_hash, api_secret = generate_api_key_and_secret()
    api_key = ApiKey(
        tenant_id=tenant.id,
        name="默认生产密钥",
        key_prefix=prefix,
        key_hash=key_hash,
        raw_key=raw_key,
        api_secret=api_secret,
        is_active=True,
    )
    db.add(api_key)
    await db.commit()

    # 3. Give Initial Trial Balance (¥50.00 = 100 free extractions)
    trial_amount = Decimal("50.0000")
    await BillingService.recharge_balance(
        db,
        tenant_id=tenant.id,
        amount=trial_amount,
        description="新用户注册赠送体验金 (100次免费额度，待审核后生效)",
        operator="SYSTEM_ONBOARDING",
    )

    return {
        "code": 0,
        "message": "开户申请已提交！您的企业租户目前处于【待审核】状态，待管理员审核开通后即可登录使用。",
        "data": {
            "tenant_id": tenant.id,
            "company_name": tenant.name,
            "contact_email": tenant.contact_email,
            "balance": float(trial_amount),
            "unit_price": 0.50,
            "is_active": False,
            "api_key": raw_key,
            "api_secret": api_secret,
        },
    }


@router.post("/login", summary="租户登录 (支持密码登录与 API Key 凭证免密登录)")
async def tenant_login(
    req: TenantLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    account = req.account.strip()

    # Case 1: Account is API Key (cg_...)
    if account.startswith("cg_"):
        auth_result = await authenticate_api_key(db, account)
        if not auth_result:
            key_prefix = account[:11] if len(account) >= 11 else account
            key_stmt = select(ApiKey, Tenant).join(Tenant, ApiKey.tenant_id == Tenant.id).where(ApiKey.key_prefix == key_prefix).limit(1)
            key_res = await db.execute(key_stmt)
            row = key_res.first()
            if row and not row[1].is_active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"code": 40301, "message": "该企业租户目前处于【待审核】状态，请联系管理员审核开通！"},
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": 40101, "message": "无效或已禁用的 API Key"},
            )
        tenant, _ = auth_result
        if not tenant.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": 40301, "message": "该企业租户目前处于【待审核】状态，请联系管理员审核开通！"},
            )
        return {
            "code": 0,
            "message": "API Key 登录成功",
            "data": {
                "token": create_access_token(tenant.id),
                "tenant_id": tenant.id,
                "tenant_name": tenant.name,
                "balance": float(Decimal(str(tenant.balance)) - Decimal(str(tenant.reserved_balance))),
                "unit_price": float(tenant.unit_price),
            },
        }

    # Case 2: Account is Email + Password
    stmt = (
        select(Tenant)
        .where(func.lower(Tenant.contact_email) == account.lower())
        .limit(2)
    )
    res = await db.execute(stmt)
    matching_tenants = list(res.scalars().all())
    if not matching_tenants:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": 40102, "message": "账号或密码错误"},
        )
    if len(matching_tenants) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": 40902, "message": "账户存在历史重复记录，请联系管理员处理后再登录"},
        )
    tenant = matching_tenants[0]

    if not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": 40301, "message": "您的企业租户目前处于【待审核】状态，请联系管理员审核开通！"},
        )



    if not req.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40002, "message": "请输入登录密码"},
        )

    stored_hash = tenant.password_hash or ""
    if not verify_password(req.password, stored_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": 40103, "message": "账号或密码错误"},
        )

    if password_needs_rehash(stored_hash):
        tenant.password_hash = hash_password(req.password)
        await db.commit()

    return {
        "code": 0,
        "message": "登录成功",
        "data": {
            "token": create_access_token(tenant.id),
            "tenant_id": tenant.id,
            "tenant_name": tenant.name,
            "balance": float(Decimal(str(tenant.balance)) - Decimal(str(tenant.reserved_balance))),
            "unit_price": float(tenant.unit_price),
        },
    }


@router.post("/admin/login", summary="管理员登录")
async def admin_login(
    req: AdminLoginRequest,
):
    import hmac
    allowed_keys = [settings.ADMIN_SECRET_KEY]
    is_valid_pwd = False
    for k in allowed_keys:
        if not k:
            continue
        try:
            if hmac.compare_digest(req.password.encode("utf-8"), k.encode("utf-8")):
                is_valid_pwd = True
                break
        except Exception:
            if req.password == k:
                is_valid_pwd = True
                break

    if req.username == "admin" and is_valid_pwd:
        token = create_access_token("admin", role="admin")

        return {
            "code": 0,
            "message": "管理员登录成功",
            "data": {
                "admin_token": token,
                "username": "admin",
                "role": "SUPER_ADMIN",
            },
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": 40104, "message": "管理员密码错误，请检查 ADMIN_SECRET_KEY"},
    )
