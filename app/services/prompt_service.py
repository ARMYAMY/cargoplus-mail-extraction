from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import PromptVersion


class PromptService:
    REQUIRED_PLACEHOLDERS = ("{{mail_subject}}", "{{mail_body}}", "{{attachments_text}}")

    @classmethod
    def validate_template(cls, content: str) -> None:
        if not content or len(content.encode("utf-8")) > 128 * 1024:
            raise ValueError("提示词不能为空且不能超过 128 KiB")
        missing = [item for item in cls.REQUIRED_PLACEHOLDERS if item not in content]
        if missing:
            raise ValueError(f"提示词缺少必要占位符: {', '.join(missing)}")

    @staticmethod
    async def get_active(db: AsyncSession) -> Optional[PromptVersion]:
        return (
            await db.execute(
                select(PromptVersion)
                .where(PromptVersion.status == "ACTIVE")
                .order_by(PromptVersion.activated_at.desc(), PromptVersion.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    @classmethod
    async def activate(cls, db: AsyncSession, prompt: PromptVersion) -> None:
        cls.validate_template(prompt.content)
        await db.execute(
            update(PromptVersion)
            .where(PromptVersion.status == "ACTIVE", PromptVersion.id != prompt.id)
            .values(status="ARCHIVED")
        )
        prompt.status = "ACTIVE"
