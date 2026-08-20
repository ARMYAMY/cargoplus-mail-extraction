import json
import logging
from typing import Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.feedback import FewShotExample

logger = logging.getLogger(__name__)

# In-memory cache for active few-shot examples: {doc_type: [examples]}
_few_shot_cache: Optional[List[Dict]] = None


class FewShotService:
    @staticmethod
    def invalidate_cache():
        global _few_shot_cache
        _few_shot_cache = None
        logger.info("FewShot cache invalidated")

    @classmethod
    async def get_active_examples(
        cls,
        db: AsyncSession,
        doc_type: Optional[str] = None,
        limit: int = 3,
    ) -> List[Dict]:
        """
        Retrieves top active Few-Shot examples sorted by priority.
        Uses in-memory cache when available.
        """
        global _few_shot_cache
        if _few_shot_cache is None:
            try:
                stmt = (
                    select(FewShotExample)
                    .where(FewShotExample.is_active.is_(True))
                    .order_by(FewShotExample.priority.desc(), FewShotExample.created_at.desc())
                )
                res = await db.execute(stmt)
                items = res.scalars().all()
                _few_shot_cache = [
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
            except Exception as e:
                logger.error("Failed to load few-shot examples from DB: %s", e)
                return []

        examples = _few_shot_cache or []
        if doc_type and doc_type != "GENERAL":
            matched = [ex for ex in examples if ex["doc_type"] == doc_type or ex["doc_type"] == "GENERAL"]
            return matched[:limit]
        return examples[:limit]

    @classmethod
    async def build_few_shot_prompt_section(
        cls,
        db: AsyncSession,
        doc_type: Optional[str] = None,
        max_examples: int = 2,
    ) -> str:
        """
        Formats active few-shot examples into an In-Context Learning prompt snippet.
        """
        examples = await cls.get_active_examples(db, doc_type=doc_type, limit=max_examples)
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
