from datetime import datetime, timedelta, timezone
from celery import Task
from sqlalchemy import or_, select
from .celery_app import celery
from .config import get_settings
from .db import SessionLocal
from .models import WebhookEvent, WebhookOutbox

settings = get_settings()

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def business_process(event: WebhookEvent) -> None:
    # Replace this function with your idempotent domain action. Keep external
    # calls protected by your own order/payment idempotency key.
    if event.event_type == "checkout.session.completed":
        print(f"Fulfill order for Checkout Session {event.object_id}")
    elif event.event_type == "payment_intent.succeeded":
        print(f"Record successful payment {event.object_id}")
    elif event.event_type == "payment_intent.payment_failed":
        print(f"Mark payment failed {event.object_id}")
    elif event.event_type == "invoice.paid":
        print(f"Record paid invoice {event.object_id}")
    elif event.event_type == "invoice.payment_failed":
        print(f"Notify about failed invoice {event.object_id}")
    elif event.event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
        print(f"Sync subscription {event.object_id}")

@celery.task(name="app.tasks.process_event", bind=True, max_retries=8)
def process_event(self: Task, event_id: str):
    with SessionLocal.begin() as db:
        event = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id).with_for_update())
        if event is None or event.status == "processed":
            return "skipped"
        cutoff = utcnow() - timedelta(seconds=settings.processing_timeout_seconds)
        if event.status == "processing" and event.processing_at and event.processing_at > cutoff:
            return "already_processing"
        event.status = "processing"
        event.processing_at = utcnow()
        event.attempts += 1
        event.last_error = None
    try:
        with SessionLocal() as db:
            event = db.get(WebhookEvent, event_id)
            if event is None:
                return "missing"
            business_process(event)
        with SessionLocal.begin() as db:
            event = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id).with_for_update())
            if event and event.status != "processed":
                event.status = "processed"
                event.processed_at = utcnow()
        return "processed"
    except Exception as exc:
        with SessionLocal.begin() as db:
            event = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id).with_for_update())
            if event:
                event.status = "queued"
                event.last_error = str(exc)[:4000]
        raise self.retry(exc=exc, countdown=min(60 * (2 ** self.request.retries), 3600))

@celery.task(name="app.tasks.dispatch_outbox")
def dispatch_outbox():
    # Publish first, then mark. A crash can publish the same event twice, but
    # never loses an event after marking it published; process_event is idempotent.
    with SessionLocal() as db:
        event_ids = list(db.scalars(select(WebhookOutbox.event_id).where(WebhookOutbox.published_at.is_(None)).order_by(WebhookOutbox.id).limit(100)))
    published = 0
    for event_id in event_ids:
        process_event.delay(event_id)
        with SessionLocal.begin() as db:
            row = db.scalar(select(WebhookOutbox).where(WebhookOutbox.event_id == event_id).with_for_update())
            if row and row.published_at is None:
                row.published_at = utcnow()
                published += 1
    return {"published": published}
