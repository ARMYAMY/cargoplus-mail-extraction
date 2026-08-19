from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest

from app.config import settings
from app.core.redis_client import get_async_redis

logger = logging.getLogger(__name__)

HTTP_REQUESTS = Counter(
    "cargoplus_http_requests_total",
    "HTTP requests handled by the API",
    ("method", "route", "status"),
)
HTTP_LATENCY = Histogram(
    "cargoplus_http_request_duration_seconds",
    "HTTP request latency",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
HTTP_IN_PROGRESS = Gauge(
    "cargoplus_http_requests_in_progress",
    "Currently executing HTTP requests",
    ("method",),
)

LLM_METRICS_KEY = "cargoplus:metrics:llm"


async def record_llm_attempt(outcome: str, duration_seconds: float) -> None:
    """Persist worker-side LLM metrics in Redis for the monitor exporter."""
    if not settings.METRICS_ENABLED:
        return
    client = get_async_redis(decode_responses=True)
    try:
        pipe = client.pipeline(transaction=False)
        pipe.hincrby(LLM_METRICS_KEY, f"attempts:{outcome}", 1)
        pipe.hincrby(LLM_METRICS_KEY, "latency_count", 1)
        pipe.hincrbyfloat(LLM_METRICS_KEY, "latency_sum", max(duration_seconds, 0.0))
        await pipe.execute()
    except Exception as exc:
        logger.debug("Unable to persist LLM metric: %s", exc)
    finally:
        await client.aclose()


def api_metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
