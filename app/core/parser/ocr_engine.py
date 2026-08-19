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


def extract_ocr_from_image(image_path: Path) -> str:
    """Extracts text lines from an image file using RapidOCR."""
    engine = get_ocr_engine()
    if not engine:
        return ""
    try:
        result, elapse_list = engine(str(image_path))
        if not result:
            return ""
        lines = [item[1] for item in result if len(item) > 1 and item[1]]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"OCR extraction failed for {image_path}: {e}")
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
