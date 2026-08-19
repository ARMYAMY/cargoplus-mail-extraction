from __future__ import annotations

from urllib.parse import urlparse

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.sentinel import Sentinel as AsyncSentinel
from redis.sentinel import Sentinel

from app.config import settings


CLIENT_OPTIONS = {"socket_connect_timeout": 2, "socket_timeout": 2}


def _sentinel_nodes() -> list[tuple[str, int]]:
    nodes: list[tuple[str, int]] = []
    for value in settings.REDIS_SENTINEL_URLS.split(","):
        value = value.strip()
        if not value:
            continue
        parsed = urlparse(value if "://" in value else f"sentinel://{value}")
        if not parsed.hostname:
            raise ValueError("Invalid REDIS_SENTINEL_URLS entry")
        nodes.append((parsed.hostname, parsed.port or 26379))
    return nodes


def get_redis(*, decode_responses: bool = False, db: int = 0) -> Redis:
    nodes = _sentinel_nodes()
    if nodes:
        sentinel = Sentinel(
            nodes,
            password=settings.REDIS_PASSWORD or None,
            **CLIENT_OPTIONS,
        )
        return sentinel.master_for(
            settings.REDIS_SENTINEL_MASTER_NAME,
            db=db,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=decode_responses,
            **CLIENT_OPTIONS,
        )
    return Redis.from_url(
        settings.CELERY_BROKER_URL,
        decode_responses=decode_responses,
        **CLIENT_OPTIONS,
    )


def get_async_redis(*, decode_responses: bool = False, db: int = 0) -> AsyncRedis:
    nodes = _sentinel_nodes()
    if nodes:
        sentinel = AsyncSentinel(
            nodes,
            password=settings.REDIS_PASSWORD or None,
            **CLIENT_OPTIONS,
        )
        return sentinel.master_for(
            settings.REDIS_SENTINEL_MASTER_NAME,
            db=db,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=decode_responses,
            **CLIENT_OPTIONS,
        )
    return AsyncRedis.from_url(
        settings.CELERY_BROKER_URL,
        decode_responses=decode_responses,
        **CLIENT_OPTIONS,
    )
