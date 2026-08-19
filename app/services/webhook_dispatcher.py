import asyncio
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.webhook_service import send_webhook_notification


async def dispatch_webhook(
    *,
    db: AsyncSession,
    task_id: str,
    callback_url: str,
    tenant_secret: str,
    payload: Dict[str, Any],
) -> str:
    """Deliver inline in tests, otherwise route webhook work to its own queue."""
    if settings.TASK_QUEUE_MODE == "celery":
        from app.celery_tasks import deliver_task_webhook

        await asyncio.to_thread(
            deliver_task_webhook.apply_async,
            args=[task_id],
            queue=settings.CELERY_WEBHOOK_QUEUE_NAME,
        )
        return "PENDING"

    delivered = await send_webhook_notification(
        db=db,
        task_id=task_id,
        callback_url=callback_url,
        secret=tenant_secret,
        payload_dict=payload,
    )
    return "SUCCESS" if delivered else "FAILED"
