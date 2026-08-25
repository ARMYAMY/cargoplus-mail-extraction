import json
import logging
from typing import Dict, List, Optional
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.feedback import FewShotExample

logger = logging.getLogger(__name__)

class FewShotService:
    DOCUMENT_TYPE_MARKERS = (
        ("BILL_OF_LADING", ("BILL OF LADING", "提单")),
        ("BOOKING_CONFIRMATION", ("BOOKING CONFIRMATION", "订舱确认")),
        ("SHIPPING_INSTRUCTION", ("SHIPPING INSTRUCTION", "SI INSTRUCTION", "补料", "托书")),
        ("COMMERCIAL_INVOICE", ("COMMERCIAL INVOICE", "商业发票")),
        ("PACKING_LIST", ("PACKING LIST", "装箱单")),
    )

    @classmethod
    def detect_document_type(cls, payload) -> str:
        parts = [getattr(payload, "mail_subject", ""), getattr(payload, "mail_body", "")]
        for attachment in getattr(payload, "attachments", []) or []:
            parts.extend([
                getattr(attachment, "filename", ""),
                getattr(attachment, "text", ""),
                getattr(attachment, "ocr_text", ""),
            ])
        content = "\n".join(str(part or "") for part in parts).upper()[:200000]
        for doc_type, markers in cls.DOCUMENT_TYPE_MARKERS:
            if any(marker.upper() in content for marker in markers):
                return doc_type
        return "GENERAL"

    @staticmethod
    def invalidate_cache():
        # Samples are queried on every extraction so changes are immediately
        # visible to all API and Celery worker processes.
        logger.debug("FewShot samples use database-backed reads; no local cache to invalidate")

    @classmethod
    async def get_active_examples(
        cls,
        db: AsyncSession,
        tenant_id: Optional[str] = None,
        doc_type: Optional[str] = None,
        limit: int = 3,
    ) -> List[Dict]:
        """
        Retrieves top active Few-Shot examples sorted by priority.

        Global examples (source_tenant_id is NULL) are available to all tenants.
        Examples created from tenant feedback are visible only to that tenant,
        preventing customer document content from crossing tenant boundaries.
        """
        safe_limit = max(1, min(int(limit), 10))
        stmt = select(FewShotExample).where(
            FewShotExample.is_active.is_(True),
            FewShotExample.lifecycle_status == "ACTIVE",
        )
        if tenant_id:
            stmt = stmt.where(
                or_(
                    FewShotExample.source_tenant_id.is_(None),
                    FewShotExample.source_tenant_id == tenant_id,
                )
            )
        else:
            stmt = stmt.where(FewShotExample.source_tenant_id.is_(None))
        if doc_type and doc_type != "GENERAL":
            stmt = stmt.where(FewShotExample.doc_type.in_([doc_type, "GENERAL"]))
        stmt = stmt.order_by(
            FewShotExample.priority.desc(),
            FewShotExample.created_at.desc(),
        ).limit(safe_limit)

        try:
            items = (await db.execute(stmt)).scalars().all()
        except Exception as exc:
            logger.error("Failed to load few-shot examples from DB: %s", exc)
            return []

        return [
            {
                "id": item.id,
                "doc_type": item.doc_type,
                "title": item.title,
                "input_excerpt": item.input_excerpt,
                "expected_output": item.expected_output,
                "priority": item.priority,
            }
            for item in items
        ]

    @classmethod
    async def build_few_shot_prompt_section(
        cls,
        db: AsyncSession,
        tenant_id: Optional[str] = None,
        doc_type: Optional[str] = None,
        max_examples: int = 2,
    ) -> str:
        """
        Formats active few-shot examples into an In-Context Learning prompt snippet.
        """
        examples = await cls.get_active_examples(
            db,
            tenant_id=tenant_id,
            doc_type=doc_type,
            limit=max_examples,
        )
        if not examples:
            return ""

        sections = ["\n### 历史纠错与典型单证标准示例 (Few-Shot Reference):"]
        for idx, ex in enumerate(examples, start=1):
            out_str = json.dumps(ex["expected_output"], ensure_ascii=False, indent=2)
            sections.append(
                f"\n[示例 {idx}: {ex['title']} ({ex['doc_type']})]\n"
                f"输入单证片段:\n```text\n{ex['input_excerpt'].strip()}\n```\n"
                f"期望标准抽取字段 JSON (局部参考):\n```json\n{out_str}\n```\n"
            )
        return "\n".join(sections)
