import base64
import io
import logging
from dataclasses import dataclass, field
from threading import Lock
import time
from typing import Optional

import httpx
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.system import SystemConfig

logger = logging.getLogger(__name__)

MAX_VISION_INPUT_BYTES = 15 * 1024 * 1024
MAX_VISION_PIXELS = 40_000_000
MAX_VISION_OUTPUT_CHARS = 20_000
VISION_MAX_TOKENS = 4096
DEFAULT_VISION_BUDGET_SECONDS = 120.0

DEFAULT_VISION_PROMPT = (
    "你是一个专业的国际海运单证与物流发票识别引擎。"
    "请高保真识别并转写该单证图片/扫描件中的所有文字、中英文表格和印章批注。"
    "若包含集装箱列表/箱号/封条号/船名航次/提单号/收发通信息，必须转写为 Markdown 规整表格，"
    "务必保持数字、代码、日期的准确性，不要遗漏模糊字样。"
    "请直接输出识别转写出的单证文本内容，不要添加额外的寒暄和客套话。"
)


@dataclass
class VisionBudget:
    """Task-wide limit for billable vision attempts, shared by all attachments."""

    max_attempts: int
    max_duration_seconds: float = DEFAULT_VISION_BUDGET_SECONDS
    attempts: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def try_acquire(self) -> bool:
        with self._lock:
            if (
                self.attempts >= self.max_attempts
                or time.monotonic() - self.started_at >= self.max_duration_seconds
            ):
                return False
            self.attempts += 1
            return True

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return (
                self.attempts >= self.max_attempts
                or time.monotonic() - self.started_at >= self.max_duration_seconds
            )

    def request_timeout(self) -> float:
        """Cap each request by the task-wide vision deadline."""
        elapsed = time.monotonic() - self.started_at
        remaining = max(0.0, self.max_duration_seconds - elapsed)
        return max(1.0, min(float(settings.VISION_LLM_TIMEOUT_SECONDS), remaining))


class VisionService:
    @staticmethod
    async def refresh_runtime_settings() -> None:
        """Refresh development runtime overrides in every worker before a task runs."""
        if settings.ENVIRONMENT.lower() == "production":
            # Production API and workers share immutable deployment environment
            # and Docker secrets. Runtime database overrides are intentionally
            # disabled so processes cannot drift from one another.
            return
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SystemConfig).where(
                        SystemConfig.key.in_(
                            {
                                "LLM_BASE_URL",
                                "LLM_API_KEY",
                                "LLM_MODEL",
                                "LLM_TIMEOUT_SECONDS",
                                "LLM_TEMPERATURE",
                                "VISION_LLM_ENABLED",
                                "VISION_LLM_MODEL",
                                "VISION_LLM_TIMEOUT_SECONDS",
                                "VISION_MAX_IMAGES_PER_TASK",
                            }
                        )
                    )
                )
                configs = {item.key: item.value for item in result.scalars().all()}

            base_url = configs.get("LLM_BASE_URL", "").strip()
            if base_url.startswith(("http://", "https://")):
                settings.LLM_BASE_URL = base_url.rstrip("/")
            api_key = configs.get("LLM_API_KEY", "").strip()
            if api_key:
                settings.LLM_API_KEY = api_key
            llm_model = configs.get("LLM_MODEL", "").strip()
            if llm_model:
                settings.LLM_MODEL = llm_model[:128]
            try:
                if configs.get("LLM_TIMEOUT_SECONDS"):
                    settings.LLM_TIMEOUT_SECONDS = max(
                        5, min(600, int(configs["LLM_TIMEOUT_SECONDS"]))
                    )
            except ValueError:
                logger.warning("Ignoring invalid LLM_TIMEOUT_SECONDS in system config")
            try:
                if configs.get("LLM_TEMPERATURE"):
                    settings.LLM_TEMPERATURE = max(
                        0.0, min(2.0, float(configs["LLM_TEMPERATURE"]))
                    )
            except ValueError:
                logger.warning("Ignoring invalid LLM_TEMPERATURE in system config")

            if "VISION_LLM_ENABLED" in configs:
                settings.VISION_LLM_ENABLED = configs["VISION_LLM_ENABLED"].lower() in {
                    "1",
                    "true",
                    "yes",
                }
            model = configs.get("VISION_LLM_MODEL", "").strip()
            if model:
                settings.VISION_LLM_MODEL = model[:128]
            try:
                if configs.get("VISION_LLM_TIMEOUT_SECONDS"):
                    settings.VISION_LLM_TIMEOUT_SECONDS = max(
                        5, min(300, int(configs["VISION_LLM_TIMEOUT_SECONDS"]))
                    )
            except ValueError:
                logger.warning("Ignoring invalid VISION_LLM_TIMEOUT_SECONDS in system config")
            try:
                if configs.get("VISION_MAX_IMAGES_PER_TASK"):
                    settings.VISION_MAX_IMAGES_PER_TASK = max(
                        1, min(20, int(configs["VISION_MAX_IMAGES_PER_TASK"]))
                    )
            except ValueError:
                logger.warning("Ignoring invalid VISION_MAX_IMAGES_PER_TASK in system config")
        except Exception as exc:
            logger.warning("Failed to refresh worker vision settings: %s", exc)

    @staticmethod
    def is_valid_document_image(
        image_bytes: bytes,
        min_dim: int = 120,
        min_bytes: int = 200,
    ) -> bool:
        """
        Filters out tiny decoration icons, logos, dividers (< 120px).
        Returns True if image is likely a scanned document page or substantive screenshot.
        """
        if (
            not image_bytes
            or len(image_bytes) < min_bytes
            or len(image_bytes) > MAX_VISION_INPUT_BYTES
        ):
            return False
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                w, h = img.size
                if w < min_dim or h < min_dim or w * h > MAX_VISION_PIXELS:
                    return False
                return True
        except Exception:
            return False

    @staticmethod
    def _local_ocr(image_bytes: bytes) -> str:
        try:
            from app.core.parser.ocr_engine import extract_ocr_from_bytes
            return extract_ocr_from_bytes(image_bytes)
        except Exception as e:
            logger.error("RapidOCR fallback error: %s", e)
            return ""

    @staticmethod
    def optimize_image_for_vision(
        image_bytes: bytes,
        max_dim: int = 1920,
        quality: int = 85,
    ) -> Optional[bytes]:
        """
        Resizes high-res images to <= 1920px and converts to optimized JPEG.
        Keeps token consumption low while preserving crisp text readability.
        """
        if not image_bytes or len(image_bytes) > MAX_VISION_INPUT_BYTES:
            logger.warning("Rejecting vision image with unsafe byte size: %s", len(image_bytes or b""))
            return None
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                w, h = img.size
                if w <= 0 or h <= 0 or w * h > MAX_VISION_PIXELS:
                    logger.warning("Rejecting vision image with unsafe dimensions: %sx%s", w, h)
                    return None
                img.load()
                # Convert to RGB if RGBA, CMYK, P or 1
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                elif img.mode == "L":
                    img = img.convert("RGB")

                if w > max_dim or h > max_dim:
                    scale = min(max_dim / w, max_dim / h)
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                out_buf = io.BytesIO()
                img.save(out_buf, format="JPEG", quality=quality, optimize=True)
                return out_buf.getvalue()
        except Exception as e:
            logger.warning("Rejecting invalid image before vision request: %s", e)
            return None

    @classmethod
    async def transcribe_image_async(
        cls,
        image_bytes: bytes,
        filename_hint: str = "",
        custom_base_url: Optional[str] = None,
        custom_api_key: Optional[str] = None,
        custom_model: Optional[str] = None,
        custom_timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> str:
        """
        Asynchronously transcribes document image using Vision LLM (SenseTime / OpenAI Vision format).
        Seamlessly falls back to local RapidOCR if vision is disabled or fails.
        """
        is_enabled = enabled if enabled is not None else settings.VISION_LLM_ENABLED
        base_url = (custom_base_url or settings.LLM_BASE_URL).rstrip("/")
        api_key = custom_api_key if custom_api_key is not None else settings.LLM_API_KEY
        model = custom_model or settings.VISION_LLM_MODEL
        timeout = custom_timeout if custom_timeout is not None else settings.VISION_LLM_TIMEOUT_SECONDS

        # Validate and normalize before either remote or local OCR.
        optimized_bytes = cls.optimize_image_for_vision(image_bytes)
        if optimized_bytes is None:
            return ""
        if not is_enabled or not api_key or not api_key.strip():
            logger.debug("Vision LLM is not enabled or API key empty; using RapidOCR fallback")
            return cls._local_ocr(optimized_bytes)

        b64_image = base64.b64encode(optimized_bytes).decode("utf-8")
        image_data_url = f"data:image/jpeg;base64,{b64_image}"

        # 3. Call OpenAI-compatible Vision completions API
        endpoint = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DEFAULT_VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": VISION_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                res = await client.post(endpoint, json=payload, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        content = choices[0]["message"].get("content", "")
                        if content and content.strip():
                            logger.info(
                                f"Vision transcription succeeded for {filename_hint} via model {model}"
                            )
                            return content.strip()[:MAX_VISION_OUTPUT_CHARS]
                logger.warning(
                    f"Vision API returned status {res.status_code}: {res.text[:200]}, falling back to RapidOCR"
                )
        except Exception as exc:
            logger.warning(
                f"Vision transcription failed for {filename_hint} ({exc}), falling back to RapidOCR"
            )

        # Fallback to local CPU RapidOCR
        return cls._local_ocr(optimized_bytes)

    @classmethod
    def transcribe_image_sync(
        cls,
        image_bytes: bytes,
        filename_hint: str = "",
        custom_base_url: Optional[str] = None,
        custom_api_key: Optional[str] = None,
        custom_model: Optional[str] = None,
        custom_timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> str:
        """
        Synchronous wrapper for document image transcription with RapidOCR fallback.
        """
        is_enabled = enabled if enabled is not None else settings.VISION_LLM_ENABLED
        base_url = (custom_base_url or settings.LLM_BASE_URL).rstrip("/")
        api_key = custom_api_key if custom_api_key is not None else settings.LLM_API_KEY
        model = custom_model or settings.VISION_LLM_MODEL
        timeout = custom_timeout if custom_timeout is not None else settings.VISION_LLM_TIMEOUT_SECONDS

        optimized_bytes = cls.optimize_image_for_vision(image_bytes)
        if optimized_bytes is None:
            return ""
        if not is_enabled or not api_key or not api_key.strip():
            return cls._local_ocr(optimized_bytes)

        b64_image = base64.b64encode(optimized_bytes).decode("utf-8")
        image_data_url = f"data:image/jpeg;base64,{b64_image}"

        endpoint = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DEFAULT_VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": VISION_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=float(timeout)) as client:
                res = client.post(endpoint, json=payload, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        content = choices[0]["message"].get("content", "")
                        if content and content.strip():
                            return content.strip()[:MAX_VISION_OUTPUT_CHARS]
                logger.warning(
                    f"Vision API sync returned status {res.status_code}, falling back to RapidOCR"
                )
        except Exception as exc:
            logger.warning(
                f"Vision transcription sync failed ({exc}), falling back to RapidOCR"
            )

        return cls._local_ocr(optimized_bytes)
