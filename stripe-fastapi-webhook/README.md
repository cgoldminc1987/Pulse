# Production FastAPI + Celery Stripe webhook receiver

This service receives Stripe webhooks with FastAPI, verifies signatures against the **raw request body**, rate-limits requests with Redis, stores each accepted event in a PostgreSQL inbox plus transactional outbox, and processes it asynchronously with Celery over Redis.

## Guarantees and boundaries

- Invalid or unsigned requests return `400`.
- Test/live mode is checked against `STRIPE_LIVEMODE`.
- Only configured event types are accepted for processing.
- The event ID is unique in PostgreSQL, so duplicate deliveries are acknowledged without inserting another job.
- The inbox and outbox rows commit in one database transaction. Celery Beat publishes unsent outbox rows.
- Worker processing claims rows with a database lock and retries failures with exponential backoff.
- A publish crash can enqueue the same event more than once; the worker checks the event state and the business operation must also be idempotent.
- `/healthz` is a liveness endpoint. Add a separate authenticated/readiness check for your deployment if needed.

The `business_process()` function is intentionally a safe placeholder. Replace it with your domain logic and add integration tests against your order, entitlement, or accounting database. Do not treat a browser success redirect as payment confirmation; use the webhook event.

## Run locally

```bash
cp .env.example .env
# Put the endpoint secret from `stripe listen` in .env.
docker compose up --build
```

Forward local events:

```bash
stripe listen --forward-to localhost:8000/webhook/stripe
stripe trigger payment_intent.succeeded
```

The secret printed by `stripe listen` is for that CLI forwarding session. A Dashboard endpoint has a different `whsec_...` secret. Never commit `.env` or put secrets in source code.

## Production setup

1. Use a managed PostgreSQL database and Redis with TLS, authentication, backups, monitoring, and network restrictions.
2. Set `DATABASE_URL`, `REDIS_URL`, and the Dashboard endpoint’s `STRIPE_WEBHOOK_SECRET` through a secret manager. Use `STRIPE_LIVEMODE=true` only for the live endpoint and use a separate deployment/configuration for test mode.
3. Put the API behind an HTTPS load balancer. Stripe requires HTTPS for publicly accessible live webhook endpoints.
4. Configure the Stripe endpoint to send only the event types you need.
5. Run the included Alembic migrations in CI/CD before starting API and worker processes. The API no longer creates tables at startup.
6. Run API replicas, Celery workers, and exactly one Celery Beat scheduler. Monitor queue depth, event age, failed tasks, and webhook delivery status.
7. Keep Stripe signature verification as the authentication mechanism for `/webhook/stripe`; Stripe does not send an arbitrary API-key header. The internal replay endpoint uses a separate hashed API key.
8. Set log redaction and do not log full payloads, client secrets, payment details, or signing secrets.

## Important implementation details

The endpoint does **not** call `request.json()` before verification. It passes `await request.body()` directly to `stripe.Webhook.construct_event(...)`. Any JSON parsing, reserialization, whitespace change, or middleware that consumes the body before verification can invalidate the signature.

## API keys and rate limiting

Do not require `X-API-Key` on `/webhook/stripe` when Stripe calls it directly. Stripe authenticates this route with the endpoint-specific `Stripe-Signature` and `whsec_...` secret. Requiring an application API key would cause genuine Stripe deliveries to fail. If a gateway in front of the service injects an API key, that gateway becomes part of the trust boundary and must be tightly controlled.

The example includes `/internal/webhook/replay/{event_id}` for controlled operational replays. It requires `X-API-Key`, compares a SHA-256 digest with the configured hash using constant-time comparison, and rate-limits the key through Redis. Generate a high-entropy key and store only its digest:

```bash
python - <<'PY'
import hashlib, secrets
key = secrets.token_urlsafe(32)
print("API key (store once):", key)
print("INTERNAL_API_KEY_HASHES:", hashlib.sha256(key.encode()).hexdigest())
PY
```

Put only the printed digest in the secret manager or `.env`:

```dotenv
INTERNAL_API_KEY_HASHES=hex_digest_for_key_1,hex_digest_for_key_2
RATE_LIMIT_REQUESTS=120
RATE_LIMIT_WINDOW_SECONDS=60
```

Multiple hashes allow key rotation. Add the new digest, deploy, move callers to the new key, then remove the old digest. Never log, commit, or put the raw API key in a URL. Call the protected endpoint as follows:

```bash
curl -X POST \
  -H "X-API-Key: $INTERNAL_API_KEY" \
  https://api.example.com/internal/webhook/replay/evt_123
```

The Stripe webhook is rate-limited per client address. The internal replay endpoint is rate-limited per API-key digest. When Redis is unavailable, both routes return `503` rather than silently bypassing the limiter. A `429` response includes `Retry-After`; Stripe can retry a webhook that was not accepted.

If the service is behind a reverse proxy, set `TRUSTED_PROXY_HOPS` only to the number of proxies you operate and configure to overwrite `X-Forwarded-For`. Never trust arbitrary client-supplied forwarding headers. With the default `0`, the service uses the direct TCP peer address.

Rate limiting is not a replacement for network controls. In production, also use TLS, a WAF/load balancer, request-body size limits at the edge, Redis authentication/TLS, secret-manager storage, monitoring, and alerting. Keep the event allowlist narrow; wildcard `ALLOWED_EVENT_TYPES=*` is for local exploration only.

The endpoint returns `200` after the event is durably stored, not after business processing completes. This avoids Stripe timing out while order fulfillment or external API calls run. Failed worker processing changes the event back to `queued` and Celery retries it. Events stuck in `processing` longer than `PROCESSING_TIMEOUT_SECONDS` can be reclaimed by a later delivery/task; add an operational reaper if you need automatic recovery without a duplicate delivery.

For the requested local “all events” mode, set `ALLOWED_EVENT_TYPES=*`. The handler still verifies the signature and mode before accepting an event, but wildcard mode is intentionally unsuitable for production because it increases storage, processing, and operational load. Production should use a narrow allowlist.

The outbox dispatcher publishes to Celery before marking a row as published. If it crashes after dispatch but before marking, the event can be dispatched again; the worker's state check makes that safe. If the broker call fails, the row remains unsent for the next Beat pass. Multiple Beat instances can still publish duplicates, so run one scheduler and retain idempotency in the worker and domain action.

## Tests

Create a virtual environment and run:

```bash
python -m pip install -r requirements.txt
pytest -q
```

The included tests validate the raw-body signature path, missing/invalid signatures, mode checks, event filtering, and the persistence contract. Run integration tests against disposable PostgreSQL and Redis before deploying; SQLite is not an adequate substitute for the row locks and PostgreSQL JSONB/unique constraints used here.

## Event handling checklist

- Verify the exact raw body, full `Stripe-Signature` header, and endpoint-specific `whsec_...` secret.
- Keep the timestamp tolerance enabled to reduce replay risk.
- Deduplicate by event ID, and for critical effects also protect the underlying business object/action with its own unique key.
- Return a quick `2xx` only after durable acceptance.
- Return `5xx` when the event was not durably accepted so Stripe can retry.
- Handle unknown valid event types safely.
- Use HTTPS and synchronized server clocks.

## Key files

- `app/main.py`: FastAPI endpoint and signature verification.
- `app/security.py`: Redis rate limiter and constant-time hashed API-key verification.
- `app/models.py`: PostgreSQL inbox/outbox schema.
- `app/tasks.py`: Celery dispatcher and retryable worker.
- `app/celery_app.py`: Celery reliability settings and Beat schedule.
- `alembic/env.py`: Alembic environment wired to application metadata and `DATABASE_URL`.
- `alembic/versions/20261016_0001_initial_webhook_tables.py`: Initial PostgreSQL schema migration.
- `docker-compose.yml`: Local service topology.

## Alembic migrations

The migration environment imports `Base.metadata` from `app.models`, reads `DATABASE_URL`, and enables type and server-default comparison for autogeneration. The webhook secret is not required to run migrations; it is required only by the API.

Install the pinned dependencies, set `DATABASE_URL`, and inspect the current revision:

```bash
alembic current
alembic heads
```

Create the schema in an empty PostgreSQL database:

```bash
alembic upgrade head
```

The local Compose stack runs this automatically through the one-shot `migrate` service before `api`, `worker`, and `beat` start:

```bash
docker compose up --build
docker compose run --rm migrate alembic current
```

When models change, first generate a candidate revision:

```bash
alembic revision --autogenerate -m "describe the schema change"
```

Review the generated file manually. Alembic cannot infer every semantic change safely, especially column renames, data backfills, constraint changes, enum changes, or destructive operations. Add explicit data migration steps where needed, test both directions where a downgrade is supported, then run:

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

In production, run `alembic upgrade head` exactly once as a release or migration job, not concurrently from every API replica. The Compose `migrate` service uses `service_completed_successfully` so application containers wait for a successful migration. The initial migration creates the two webhook tables, PostgreSQL JSONB payload storage, timestamp defaults, primary/unique constraints, and indexes used by the inbox and outbox queries.
