import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.models.task import WebhookLog

import ipaddress
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_safe_webhook_url(url: str) -> bool:
    """
    Validates webhook URL to protect against SSRF (Server-Side Request Forgery).
    Rejects non-HTTP(S), localhost, loopback, private RFC1918, link-local, and multicast IP targets.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"}:
            return False
        if settings.ENVIRONMENT.lower() == "production" and parsed.scheme != "https":
            return False
        if parsed.username or parsed.password:
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        # Disallow loopback names
        if hostname.lower() in {"localhost", "localhost.localdomain", "127.0.0.1", "::1"}:
            return False

        # Check every A and AAAA answer. A hostname is accepted only if every current
        # address is globally routable; DNS failures fail closed.
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
        }
        if not addresses:
            return False
        for ip_str in addresses:
            ip_obj = ipaddress.ip_address(ip_str)
            if not ip_obj.is_global:
                logger.warning("Blocked webhook target resolving to non-public IP %s", ip_str)
                return False

        return True
    except (OSError, ValueError) as e:
        logger.warning("Invalid or unresolvable webhook URL: %s", e)
        return False


def generate_webhook_signature(secret: str, timestamp: int, payload_str: str) -> str:
    """Generates HMAC-SHA256 signature over timestamp.payload."""
    message = f"{timestamp}.{payload_str}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def send_webhook_notification(
    db: AsyncSession,
    task_id: str,
    callback_url: str,
    secret: str,
    payload_dict: Dict[str, Any],
) -> bool:
    """
    Dispatches a Webhook notification with HMAC-SHA256 signature and retry logic.
    """
    if not callback_url or not secret:
        return False

    # SSRF Protection Check
    if not await asyncio.to_thread(is_safe_webhook_url, callback_url):
        logger.error("Webhook delivery aborted for unsafe callback URL")
        return False


    payload_json = json.dumps(payload_dict, ensure_ascii=False)
    timestamp = int(time.time() * 1000)
    signature = generate_webhook_signature(secret, timestamp, payload_json)

    headers = {
        "Content-Type": "application/json",
        "X-Timestamp": str(timestamp),
        "X-Signature-SHA256": signature,
        "User-Agent": "CargoPlus-Webhook/1.0",
    }

    last_status_code = None
    last_response_body = ""
    success = False
    attempts_made = 0

    async with httpx.AsyncClient(
        timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        for attempt in range(1, settings.WEBHOOK_MAX_RETRIES + 1):
            attempts_made = attempt
            try:
                # Re-resolve before every attempt to reduce the DNS-rebinding window.
                if not await asyncio.to_thread(is_safe_webhook_url, callback_url):
                    last_response_body = "Callback URL no longer resolves exclusively to public addresses"
                    break
                resp = await client.post(callback_url, content=payload_json, headers=headers)
                last_status_code = resp.status_code
                last_response_body = resp.text[:500]
                if 200 <= resp.status_code < 300:
                    success = True
                    logger.info("Webhook delivered successfully for task %s", task_id)
                    break
                else:
                    logger.warning("Webhook attempt %s returned HTTP %s", attempt, resp.status_code)
            except Exception as e:
                last_response_body = str(e)[:500]
                logger.warning("Webhook attempt %s failed: %s", attempt, e)

            if attempt < settings.WEBHOOK_MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)

    # Record webhook log in DB
    try:
        wh_log = WebhookLog(
            task_id=task_id,
            url=callback_url,
            payload=payload_json,
            signature=signature,
            status_code=last_status_code,
            response_body=last_response_body,
            attempt_count=attempts_made,
        )
        db.add(wh_log)
        await db.commit()
    except Exception as log_err:
        await db.rollback()
        logger.error(f"Failed to record webhook log: {log_err}")

    return success
