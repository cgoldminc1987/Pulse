import json
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
import stripe
from .config import get_settings
from .db import SessionLocal
from .models import WebhookEvent, WebhookOutbox
from .security import RateLimiter, valid_internal_api_key
from .tasks import process_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stripe_webhook")
settings = get_settings()
redis = Redis.from_url(settings.redis_url, decode_responses=True)
rate_limiter = RateLimiter(redis)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis.aclose()

app = FastAPI(title="Stripe Webhook Receiver", docs_url=None, redoc_url=None, lifespan=lifespan)

def client_key(request: Request) -> str:
    # Only trust X-Forwarded-For when the deployment explicitly declares a
    # trusted proxy hop. Otherwise use the direct peer address.
    if settings.trusted_proxy_hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        addresses = [x.strip() for x in forwarded.split(",") if x.strip()]
        if len(addresses) >= settings.trusted_proxy_hops:
            return addresses[-settings.trusted_proxy_hops]
    return request.client.host if request.client else "unknown"

async def enforce_rate_limit(bucket: str, retry_status: int = 429):
    try:
        result = await rate_limiter.check(bucket)
    except Exception:
        logger.exception("Rate limiter unavailable")
        raise HTTPException(status_code=503, detail="Rate limiter unavailable")
    if not result.allowed:
        raise HTTPException(status_code=retry_status, detail="Rate limit exceeded", headers={"Retry-After": str(result.retry_after)})

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")):
    await enforce_rate_limit(f"stripe:{client_key(request)}")
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, settings.stripe_webhook_secret, tolerance=settings.webhook_tolerance_seconds)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    if bool(event.livemode) != settings.stripe_livemode:
        raise HTTPException(status_code=400, detail="Unexpected event mode")
    if not settings.accepts_all_event_types and event.type not in settings.event_types:
        return {"received": True, "ignored": True}
    body = json.loads(payload)
    obj = body.get("data", {}).get("object", {})
    event_id = body.get("id")
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event ID")
    try:
        with SessionLocal.begin() as db:
            db.add(WebhookEvent(event_id=event_id, event_type=event.type, object_id=obj.get("id"), livemode=bool(event.livemode), payload=body))
            db.add(WebhookOutbox(event_id=event_id))
    except IntegrityError:
        # Unique event_id means this is a duplicate delivery. Acknowledge it.
        logger.info("Duplicate Stripe event acknowledged: %s", event_id)
        return {"received": True, "duplicate": True}
    logger.info("Accepted Stripe event %s (%s)", event_id, event.type)
    return {"received": True}

@app.post("/internal/webhook/replay/{event_id}")
async def replay_webhook(event_id: str, request: Request, api_key: str | None = Header(default=None, alias="X-API-Key")):
    if not valid_internal_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key", headers={"WWW-Authenticate": "ApiKey"})
    await enforce_rate_limit(f"internal:{digest_for_rate_limit(api_key)}")
    with SessionLocal() as db:
        event = db.get(WebhookEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
    process_event.delay(event_id)
    return {"accepted": True, "event_id": event_id}

def digest_for_rate_limit(raw_key: str) -> str:
    # Do not put the raw API key into Redis keys or logs.
    import hashlib
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
