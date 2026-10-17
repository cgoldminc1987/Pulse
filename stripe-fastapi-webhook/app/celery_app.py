from celery import Celery
from .config import get_settings

settings = get_settings()
celery = Celery("stripe_webhooks", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    timezone="UTC", enable_utc=True, task_acks_late=True,
    task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={"dispatch-webhook-outbox": {"task": "app.tasks.dispatch_outbox", "schedule": 5.0}},
)
