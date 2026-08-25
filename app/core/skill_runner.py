import asyncio
import json
import logging
from pathlib import Path
import re
import random
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional
import httpx
from app.config import settings
from app.core.observability import record_llm_attempt
from app.schemas.task import SkillV3InputPayload, AttachmentInput
from app.schemas.cargo_v3 import CargoV3Output

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

    def build_extract_prompt(
        self,
        payload: SkillV3InputPayload,
        few_shot_snippet: str = "",
        prompt_template: Optional[str] = None,
    ) -> str:
        attachments_text = self.format_attachments_text(payload.attachments)
        prompt = prompt_template or self.extract_prompt_template
        prompt = prompt.replace("{{mail_subject}}", payload.mail_subject or "无主题")
        prompt = prompt.replace("{{mail_body}}", payload.mail_body or "无正文")
        prompt = prompt.replace("{{attachments_text}}", attachments_text)
        if few_shot_snippet and few_shot_snippet.strip():
            prompt = prompt + "\n\n" + few_shot_snippet.strip()
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

    @staticmethod
    def _parse_json_object_response(content: str, require_cargo_fields: bool = True) -> Dict[str, Any]:
        """Parse provider responses that may contain fences or multiple JSON values.

        Some OpenAI-compatible providers occasionally prepend a small metadata JSON
        object before the requested Cargo JSON.  Selecting the object with the most
        Cargo V3 fields is deterministic and avoids treating the entire extraction as
        empty merely because ``json.loads`` reports trailing data.
        """
        cleaned = SkillRunner._clean_json_response(content)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                nested = parsed.get("final_json")
                parsed = nested if isinstance(nested, dict) else parsed
                if not require_cargo_fields or (set(parsed) & set(CargoV3Output.model_fields)):
                    return parsed
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
        cargo_fields = set(CargoV3Output.model_fields)
        candidates: List[Dict[str, Any]] = []
        cursor = 0
        while cursor < len(cleaned):
            start = cleaned.find("{", cursor)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(cleaned, start)
            except json.JSONDecodeError:
                cursor = start + 1
                continue
            if isinstance(value, dict):
                nested = value.get("final_json")
                candidates.append(nested if isinstance(nested, dict) else value)
            cursor = max(end, start + 1)
        if not candidates:
            raise ValueError("模型响应中没有可解析的 JSON 对象")
        best = max(candidates, key=lambda item: (len(set(item) & cargo_fields), len(item)))
        if require_cargo_fields and not (set(best) & cargo_fields):
            raise ValueError("模型响应中的 JSON 不包含 CargoPlus 字段")
        return best

    async def call_llm(
        self,
        prompt: str,
        model_override: Optional[str] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
        allow_fallback: bool = True,
        timeout_seconds: Optional[float] = None,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> str:
        """
        Invokes LLM API (SenseTime / OpenAI compatible) with temperature=0 and JSON mode.
        """
        model_name = model_override or settings.LLM_MODEL
        base_url = settings.LLM_BASE_URL.rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        api_key = settings.LLM_API_KEY.strip()

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a specialized cargo mail extraction agent. Always return valid JSON without extra text.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": settings.LLM_TEMPERATURE,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Execute with configured timeout and exponential backoff retries.
        # Callers such as the interactive prompt lab can use a smaller retry
        # budget so one browser request does not remain pending for minutes.
        retry_budget = settings.LLM_MAX_RETRIES if max_retries is None else max(0, max_retries)
        last_error = None
        request_timeout = float(timeout_seconds or settings.LLM_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(
            timeout=request_timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ) as client:
            for attempt in range(retry_budget + 1):
                if progress_callback:
                    await progress_callback("MAIN_MODEL_REQUEST", {
                        "attempt": attempt + 1,
                        "total_attempts": retry_budget + 1,
                        "model": model_name,
                    })
                attempt_started = time.monotonic()
                try:
                    resp = await client.post(endpoint, json=payload, headers=headers)
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
                    last_error = f"LLM Timeout ({request_timeout:g}s)"
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
        if allow_fallback and settings.LLM_FALLBACK_MODEL and settings.LLM_FALLBACK_MODEL != model_name:
            logger.info(f"Retrying with fallback model {settings.LLM_FALLBACK_MODEL}...")
            return await self.call_llm(
                prompt,
                model_override=settings.LLM_FALLBACK_MODEL,
                max_tokens=max_tokens,
                max_retries=max_retries,
                allow_fallback=False,
                timeout_seconds=timeout_seconds,
                progress_callback=progress_callback,
            )

        raise RuntimeError(f"LLM Extraction failed after {retry_budget+1} attempts. Error: {last_error}")

    async def stream_llm(
        self,
        prompt: str,
        max_tokens: int = 2200,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> AsyncIterator[str]:
        """Stream OpenAI-compatible completion deltas for interactive admin work."""
        model_name = model_override or settings.LLM_MODEL
        endpoint = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": "你是货代单证抽取规则优化专家。只返回请求指定的合法 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": settings.LLM_TEMPERATURE,
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {settings.LLM_API_KEY.strip()}",
            "Content-Type": "application/json",
        }
        stream_timeout = float(timeout_seconds or settings.LLM_TIMEOUT_SECONDS)
        timeout = httpx.Timeout(
            connect=min(stream_timeout, 20.0),
            read=stream_timeout,
            write=30.0,
            pool=20.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                response.raise_for_status()
                yielded_content = False
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    # Most providers use SSE (``data: {...}``), while a few
                    # OpenAI-compatible gateways return one JSON object per line
                    # even when ``stream=true`` was requested.
                    data = line[5:].strip() if line.startswith("data:") else line.strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") or []
                    content = ""
                    if choices:
                        choice = choices[0] or {}
                        delta = choice.get("delta") or {}
                        content = delta.get("content") or choice.get("text") or ""
                        # Some gateways send the final answer in ``message``
                        # rather than delta chunks.  Only consume it when no
                        # content delta has been emitted, avoiding duplication.
                        if not content and not yielded_content:
                            content = (choice.get("message") or {}).get("content") or ""
                    if not content and not yielded_content:
                        content = event.get("output_text") or event.get("text") or ""
                    if content:
                        yielded_content = True
                        yield str(content)

    async def extract_draft_json(
        self,
        payload: SkillV3InputPayload,
        few_shot_snippet: str = "",
        prompt_template: Optional[str] = None,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        Executes Prompt extraction against LLM and parses JSON.
        """
        prompt = self.build_extract_prompt(
            payload,
            few_shot_snippet=few_shot_snippet,
            prompt_template=prompt_template,
        )
        response_text = await self.call_llm(prompt, progress_callback=progress_callback)
        if progress_callback:
            await progress_callback("PARSING_MODEL_JSON", {})
        try:
            draft_json = self._parse_json_object_response(response_text)
        except (json.JSONDecodeError, ValueError) as jde:
            logger.warning("Initial LLM JSON parse failed: %s", jde)
            # Try repairing with validate prompt
            if self.validate_prompt_template:
                cleaned_json_str = self._clean_json_response(response_text)
                val_prompt = self.build_validate_prompt(cleaned_json_str, [f"JSON syntax error: {str(jde)}"])
                repaired_text = await self.call_llm(val_prompt, progress_callback=progress_callback)
                if progress_callback:
                    await progress_callback("PARSING_MODEL_JSON", {"repair": True})
                draft_json = self._parse_json_object_response(repaired_text)
            else:
                raise ValueError(f"Invalid JSON returned from LLM: {jde}")

        if not isinstance(draft_json, dict):
            draft_json = {}

        return draft_json


default_skill_runner = SkillRunner()
