import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_ocr_instance = None


def get_ocr_engine():
    global _ocr_instance
    if _ocr_instance is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_instance = RapidOCR()
            logger.info("RapidOCR initialized successfully.")
        except Exception as e:
            logger.warning(f"Failed to initialize RapidOCR: {e}. OCR functionality will be limited.")
            _ocr_instance = False
    return _ocr_instance if _ocr_instance is not False else None


def extract_ocr_from_image(image_path: Path, vision_budget=None) -> str:
    """
    Extracts text lines from an image file using Vision LLM (with RapidOCR fallback).
    """
    try:
        from app.services.vision_service import VisionService
        if vision_budget is not None and not vision_budget.try_acquire():
            return ""
        timeout = vision_budget.request_timeout() if vision_budget is not None else None
        return VisionService.transcribe_image_sync(
            image_path.read_bytes(),
            filename_hint=image_path.name,
            custom_timeout=timeout,
        )
    except Exception as e:
        logger.warning("Image OCR rejected for %s: %s", image_path, e)
        return ""


def extract_ocr_from_bytes(image_bytes: bytes) -> str:
    """Extracts text lines from image bytes using RapidOCR."""
    engine = get_ocr_engine()
    if not engine:
        return ""
    try:
        result, elapse_list = engine(image_bytes)
        if not result:
            return ""
        lines = [item[1] for item in result if len(item) > 1 and item[1]]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"OCR extraction failed for image bytes: {e}")
        return ""
