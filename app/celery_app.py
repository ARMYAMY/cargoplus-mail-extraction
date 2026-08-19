from celery import Celery

from app.config import settings


celery_app = Celery(
    "cargoplus",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.celery_tasks"],
)

visibility_timeout = settings.TASK_LEASE_SECONDS + 300
broker_transport_options = {"visibility_timeout": visibility_timeout}
result_backend_transport_options = {"visibility_timeout": visibility_timeout}
if settings.REDIS_SENTINEL_URLS:
    sentinel_options = {
        "master_name": settings.REDIS_SENTINEL_MASTER_NAME,
        "sentinel_kwargs": {"password": settings.REDIS_PASSWORD or None},
    }
    broker_transport_options.update(sentinel_options)
    result_backend_transport_options.update(sentinel_options)
celery_app.conf.update(
    task_default_queue=settings.CELERY_QUEUE_NAME,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=3600,
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=settings.TASK_TIMEOUT_SECONDS + 120,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options=broker_transport_options,
    result_backend_transport_options=result_backend_transport_options,
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_eager_propagates=True,
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "recover-stale-cargo-tasks": {
            "task": "cargoplus.recover_stale_tasks",
            "schedule": settings.TASK_RECOVERY_INTERVAL_SECONDS,
            "options": {"queue": settings.CELERY_QUEUE_NAME},
        }
    },
)
