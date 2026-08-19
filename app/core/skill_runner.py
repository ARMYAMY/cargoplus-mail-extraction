import asyncio
import json
import logging
from pathlib import Path
import re
import random
import time
from typing import Any, Dict, List, Optional
import httpx
from app.config import settings
from app.core.observability import record_llm_attempt
from app.schemas.task import SkillV3InputPayload, AttachmentInput

logger = logging.getLogger(__name__)

JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*([\s\S]*?)\s*```', re.IGNORECASE)


class NonRetryableLLMError(RuntimeError):
    pass


DEFAULT_EXTRACT_PROMPT = """你是一个专业的国际海运货代单证结构化抽取专家。
请从以下邮件与单证中精准抽取 57 个核心字段，输出符合 CargoPlus V3 规范的标准 JSON。

邮件主题: {{mail_subject}}
邮件正文: {{mail_body}}
附件与单证内容:
{{attachments_text}}
"""

DEFAULT_VALIDATE_PROMPT = """请根据以下校验错误修正并完善提取的 JSON 数据:
原始 JSON:
{{raw_json}}

错误列表:
{{errors}}
"""


class SkillRunner:
    def __init__(self, skill_path: Optional[Path] = None):
        self.skill_path = skill_path or settings.skill_path
        self._load_prompts()

    def _load_prompts(self):
        extract_file = self.skill_path / "prompts" / "extract.md"
        validate_file = self.skill_path / "prompts" / "validate.md"

        if extract_file.exists():
            self.extract_prompt_template = extract_file.read_text(encoding="utf-8")
        else:
            logger.warning(f"extract.md prompt not found at {extract_file}, using built-in fallback")
            self.extract_prompt_template = DEFAULT_EXTRACT_PROMPT

        if validate_file.exists():
            self.validate_prompt_template = validate_file.read_text(encoding="utf-8")
        else:
            logger.warning(f"validate.md prompt not found at {validate_file}, using built-in fallback")
            self.validate_prompt_template = DEFAULT_VALIDATE_PROMPT

    def format_attachments_text(self, attachments: List[AttachmentInput]) -> str:
        if not attachments:
            return "无附件"

        blocks = []
        for idx, att in enumerate(attachments, 1):
            block_lines = [f"--- 附件 {idx}: {att.filename} ---"]
            if att.text and att.text.strip():
                block_lines.append("【文本内容】:\n" + att.text.strip())
            if att.tables:
                block_lines.append("【表格数据】:\n" + json.dumps(att.tables, ensure_ascii=False, indent=2))
            if att.ocr_text and att.ocr_text.strip():
                block_lines.append("【OCR识别内容】:\n" + att.ocr_text.strip())
            blocks.append("\n".join(block_lines))

        return "\n\n".join(blocks)

    def build_extract_prompt(self, payload: SkillV3InputPayload) -> str:
        attachments_text = self.format_attachments_text(payload.attachments)
        prompt = self.extract_prompt_template
        prompt = prompt.replace("{{mail_subject}}", payload.mail_subject or "无主题")
        prompt = prompt.replace("{{mail_body}}", payload.mail_body or "无正文")
        prompt = prompt.replace("{{attachments_text}}", attachments_text)
        return prompt

    def build_validate_prompt(self, raw_json_str: str, error_messages: List[str]) -> str:
        prompt = self.validate_prompt_template
        prompt = prompt.replace("{{raw_json}}", raw_json_str)
        prompt = prompt.replace("{{errors}}", "\n".join(error_messages))
        return prompt

    @staticmethod
    def _clean_json_response(content: str) -> str:
        """Strips markdown code fence ```json ... ``` if present."""
        content = content.strip()
        match = JSON_BLOCK_RE.search(content)
        if match:
            return match.group(1).strip()
        return content

    async def call_llm(
        self,
        prompt: str,
        model_override: Optional[str] = None,
    ) -> str:
        """
        Invokes LLM API (SenseTime / OpenAI compatible) with temperature=0 and JSON mode.
        """
        model_name = model_override or settings.LLM_MODEL
        url = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if settings.LLM_API_KEY:
            headers["Authorization"] = f"Bearer {settings.LLM_API_KEY}"

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "temperature": settings.LLM_TEMPERATURE,
            "response_format": {"type": "json_object"},
        }

        last_error = None
        async with httpx.AsyncClient(
            timeout=settings.LLM_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ) as client:
            for attempt in range(settings.LLM_MAX_RETRIES + 1):
                attempt_started = time.monotonic()
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            await record_llm_attempt("success", time.monotonic() - attempt_started)
                            return choices[0].get("message", {}).get("content", "").strip()
                        raise ValueError("LLM returned HTTP 200 without a completion choice")
                    elif resp.status_code == 429 or resp.status_code >= 500:
                        outcome = "rate_limited" if resp.status_code == 429 else "server_error"
                        await record_llm_attempt(outcome, time.monotonic() - attempt_started)
                        last_error = f"LLM HTTP {resp.status_code}"
                        retry_after = resp.headers.get("Retry-After", "")
                        try:
                            provider_delay = min(float(retry_after), 30.0)
                        except ValueError:
                            provider_delay = 0.0
                        delay = max(provider_delay, min(2 ** attempt, 10)) + random.uniform(0, 0.5)
                        logger.warning(
                            "LLM retryable HTTP %s on attempt %s; retrying in %.2fs",
                            resp.status_code,
                            attempt + 1,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        await record_llm_attempt("client_error", time.monotonic() - attempt_started)
                        raise NonRetryableLLMError(
                            f"LLM request rejected with HTTP {resp.status_code}"
                        )
                except httpx.TimeoutException as te:
                    await record_llm_attempt("timeout", time.monotonic() - attempt_started)
                    last_error = f"LLM Timeout ({settings.LLM_TIMEOUT_SECONDS}s)"
                    logger.warning(f"LLM timeout on attempt {attempt+1}: {te}")
                    await asyncio.sleep(min(2 ** attempt, 10) + random.uniform(0, 0.5))
                except NonRetryableLLMError:
                    raise
                except Exception as e:
                    await record_llm_attempt("exception", time.monotonic() - attempt_started)
                    last_error = str(e)
                    logger.warning(f"LLM call exception on attempt {attempt+1}: {e}")
                    await asyncio.sleep(min(2 ** attempt, 10) + random.uniform(0, 0.5))

        # Fallback to fallback model if available and different
        if settings.LLM_FALLBACK_MODEL and settings.LLM_FALLBACK_MODEL != model_name:
            logger.info(f"Retrying with fallback model {settings.LLM_FALLBACK_MODEL}...")
            return await self.call_llm(prompt, model_override=settings.LLM_FALLBACK_MODEL)

        raise RuntimeError(f"LLM Extraction failed after {settings.LLM_MAX_RETRIES+1} attempts. Error: {last_error}")

    async def extract_draft_json(self, payload: SkillV3InputPayload) -> Dict[str, Any]:
        """
        Executes Prompt extraction against LLM and parses JSON.
        """
        prompt = self.build_extract_prompt(payload)
        response_text = await self.call_llm(prompt)
        cleaned_json_str = self._clean_json_response(response_text)

        try:
            draft_json = json.loads(cleaned_json_str)
        except json.JSONDecodeError as jde:
            logger.warning("Initial LLM JSON parse failed: %s", jde)
            # Try repairing with validate prompt
            if self.validate_prompt_template:
                val_prompt = self.build_validate_prompt(cleaned_json_str, [f"JSON syntax error: {str(jde)}"])
                repaired_text = await self.call_llm(val_prompt)
                repaired_cleaned = self._clean_json_response(repaired_text)
                draft_json = json.loads(repaired_cleaned)
            else:
                raise ValueError(f"Invalid JSON returned from LLM: {jde}")

        if not isinstance(draft_json, dict):
            draft_json = {}

        return draft_json


default_skill_runner = SkillRunner()
