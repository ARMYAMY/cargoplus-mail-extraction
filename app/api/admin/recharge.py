from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.schemas.billing import BillingTransactionResponse
from app.schemas.tenant import RechargeRequest
from app.api.deps import verify_admin_access
from app.services.billing_service import BillingService

router = APIRouter(prefix="/admin/recharge", dependencies=[Depends(verify_admin_access)])


@router.post("/{tenant_id}", response_model=BillingTransactionResponse, summary="管理员为租户充值余额")
async def recharge_tenant(
    tenant_id: str,
    data: RechargeRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        tx = await BillingService.recharge_balance(
            db=db,
            tenant_id=tenant_id,
            amount=data.amount,
            description=data.description or "管理员充值",
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
