from __future__ import annotations

import hashlib
import ipaddress
import logging

from fastapi import Request

from app.config import settings
from app.core.redis_client import get_async_redis

logger = logging.getLogger(__name__)


def client_rate_limit_identity(request: Request) -> str:
    """Return a bounded, non-secret identifier for the trusted reverse-proxy client."""
    value = request.client.host if request.client else "unknown"
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            try:
                value = str(ipaddress.ip_address(forwarded))
            except ValueError:
                logger.warning("Ignoring malformed X-Forwarded-For address")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


async def consume_rate_limit(bucket: str, identity: str, limit: int, window: int) -> tuple[bool, int]:
    """Atomically consume a fixed-window allowance stored in Redis."""
    redis = get_async_redis(decode_responses=True)
    key = f"cargo:rate-limit:{bucket}:{identity}"
    try:
        async with redis.pipeline(transaction=True) as pipeline:
            pipeline.incr(key)
            pipeline.expire(key, window, nx=True)
            count, _ = await pipeline.execute()
        ttl = await redis.ttl(key)
        retry_after = ttl if isinstance(ttl, int) and ttl > 0 else window
        return int(count) <= limit, retry_after
    finally:
        await redis.aclose()
