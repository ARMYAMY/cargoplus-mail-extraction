from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, Gauge, CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select

from app.celery_app import celery_app
from app.config import settings
from app.core.observability import LLM_METRICS_KEY
from app.core.redis_client import get_redis
from app.database import AsyncSessionLocal
from app.models.billing import BillingTransaction
from app.models.task import EmailTask

app = FastAPI(title="CargoPlus Metrics Exporter", docs_url=None, redoc_url=None)


def _queue_and_worker_snapshot() -> tuple[
    dict[str, int], dict[str, int], dict[str, str], str | None
]:
    redis_client = get_redis(decode_responses=True)
    try:
        queues = {
            settings.CELERY_QUEUE_NAME: int(redis_client.llen(settings.CELERY_QUEUE_NAME)),
            settings.CELERY_WEBHOOK_QUEUE_NAME: int(
                redis_client.llen(settings.CELERY_WEBHOOK_QUEUE_NAME)
            ),
        }
        llm_metrics = redis_client.hgetall(LLM_METRICS_KEY)
        beat_last_tick = redis_client.get("cargoplus:beat:last-recovery-tick")
    finally:
        redis_client.close()

    workers = {settings.CELERY_QUEUE_NAME: 0, settings.CELERY_WEBHOOK_QUEUE_NAME: 0}
    try:
        active_queues = celery_app.control.inspect(timeout=2).active_queues() or {}
        for queue_list in active_queues.values():
            names = {item.get("name") for item in queue_list}
            for queue_name in workers:
                if queue_name in names:
                    workers[queue_name] += 1
    except Exception:
        pass
    return queues, workers, llm_metrics, beat_last_tick


@app.get("/health/live", include_in_schema=False)
async def live():
    return {"status": "alive"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    registry = CollectorRegistry()
    task_status = Gauge(
        "cargoplus_tasks",
        "Tasks currently stored by status",
        ("status",),
        registry=registry,
    )
    recent_tasks = Gauge(
        "cargoplus_tasks_recent_15m",
        "Tasks created in the last 15 minutes",
        ("status",),
        registry=registry,
    )
    queue_depth = Gauge(
        "cargoplus_queue_depth",
        "Messages waiting in each Celery queue",
        ("queue",),
        registry=registry,
    )
    workers_online = Gauge(
        "cargoplus_celery_workers_online",
        "Celery workers consuming each queue",
        ("queue",),
        registry=registry,
    )
    stale_leases = Gauge(
        "cargoplus_stale_task_leases",
        "Processing tasks whose database lease has expired",
        registry=registry,
    )
    success_latency = Gauge(
        "cargoplus_success_task_duration_seconds_avg_15m",
        "Average successful task duration over 15 minutes",
        registry=registry,
    )
    revenue = Gauge(
        "cargoplus_revenue_total_yuan",
        "Total successful extraction deductions",
        registry=registry,
    )
    llm_attempts = Gauge(
        "cargoplus_llm_attempts_total",
        "LLM attempts persisted by outcome",
        ("outcome",),
        registry=registry,
    )
    llm_latency_sum = Gauge(
        "cargoplus_llm_attempt_duration_seconds_sum",
        "Cumulative LLM attempt latency",
        registry=registry,
    )
    llm_latency_count = Gauge(
        "cargoplus_llm_attempt_duration_seconds_count",
        "Number of timed LLM attempts",
        registry=registry,
    )
    backup_age = Gauge(
        "cargoplus_postgres_backup_age_seconds",
        "Seconds since the last verified PostgreSQL backup, or -1 when absent",
        registry=registry,
    )
    redis_backup_age = Gauge(
        "cargoplus_redis_backup_age_seconds",
        "Seconds since the last verified Redis RDB backup, or -1 when absent",
        registry=registry,
    )
    restore_drill_age = Gauge(
        "cargoplus_postgres_restore_drill_age_seconds",
        "Seconds since the last successful restore drill, or -1 when absent",
        registry=registry,
    )
    beat_tick_age = Gauge(
        "cargoplus_celery_beat_tick_age_seconds",
        "Seconds since the last recovery schedule tick, or -1 when absent",
        registry=registry,
    )

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(minutes=15)
    async with AsyncSessionLocal() as db:
        status_rows = (
            await db.execute(select(EmailTask.status, func.count()).group_by(EmailTask.status))
        ).all()
        recent_rows = (
            await db.execute(
                select(EmailTask.status, func.count())
                .where(EmailTask.created_at >= recent_cutoff)
                .group_by(EmailTask.status)
            )
        ).all()
        expired_count = (
            await db.execute(
                select(func.count()).select_from(EmailTask).where(
                    EmailTask.status == "PROCESSING",
                    EmailTask.lease_expires_at < now,
                )
            )
        ).scalar_one()
        avg_duration_ms = (
            await db.execute(
                select(func.avg(EmailTask.duration_ms)).where(
                    EmailTask.status == "SUCCESS",
                    EmailTask.completed_at >= recent_cutoff,
                )
            )
        ).scalar_one_or_none()
        total_revenue = (
            await db.execute(
                select(func.coalesce(func.sum(BillingTransaction.amount), 0)).where(
                    BillingTransaction.type == "DEDUCTION"
                )
            )
        ).scalar_one()

    for status in ("PENDING", "PROCESSING", "SUCCESS", "FAILED"):
        task_status.labels(status=status).set(0)
        recent_tasks.labels(status=status).set(0)
    for status, count in status_rows:
        task_status.labels(status=status).set(count)
    for status, count in recent_rows:
        recent_tasks.labels(status=status).set(count)
    stale_leases.set(expired_count)
    success_latency.set(float(avg_duration_ms or 0) / 1000)
    revenue.set(float(total_revenue or 0))

    queues, workers, llm, beat_last_tick = await asyncio.to_thread(
        _queue_and_worker_snapshot
    )
    for queue_name, count in queues.items():
        queue_depth.labels(queue=queue_name).set(count)
    for queue_name, count in workers.items():
        workers_online.labels(queue=queue_name).set(count)
    for key, value in llm.items():
        if key.startswith("attempts:"):
            llm_attempts.labels(outcome=key.split(":", 1)[1]).set(float(value))
    llm_latency_sum.set(float(llm.get("latency_sum", 0)))
    llm_latency_count.set(float(llm.get("latency_count", 0)))

    now_epoch = now.timestamp()
    try:
        beat_tick_age.set(max(0, now_epoch - float(beat_last_tick)))
    except (TypeError, ValueError):
        beat_tick_age.set(-1)
    for file_name, metric in (
        (".last_backup_success", backup_age),
        (".last_redis_backup_success", redis_backup_age),
        (".last_restore_drill_success", restore_drill_age),
    ):
        try:
            timestamp = float((Path("/backups") / file_name).read_text(encoding="utf-8").strip())
            metric.set(max(0, now_epoch - timestamp))
        except (OSError, ValueError):
            metric.set(-1)

    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
