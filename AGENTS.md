# AGENTS.md — working notes for this repository

## Layout
- `stripe-fastapi-webhook/` — the entire application: FastAPI receiver (`app/`), Alembic migrations (`alembic/`), pytest suite (`tests/`).
- `gemini-code-*.txt` — an unrelated uploaded HTML mock; not part of the service.
- `docker-compose.base44.yml` + `.base44/Dockerfile.api` — the Base44 dev/sandbox stack (below). The service's own `Dockerfile` / `docker-compose.yml` are the production-shaped originals and are left untouched.

## Run it (sandbox)
```bash
docker compose -f docker-compose.base44.yml up -d --build
```

Non-obvious things:
- The app services run **from the bind mount** `./stripe-fastapi-webhook:/srv` with `uvicorn --reload` (plus `WATCHFILES_FORCE_POLLING`). `.base44/Dockerfile.api` installs the pinned `requirements.txt` only — no app code — so rebuild (`--build`) only after changing `requirements.txt`; source edits need no rebuild.
- Tables are created by the one-shot `migrate` service (`alembic upgrade head`); `api`, `worker` and `beat` wait for it with `service_completed_successfully`. The application never creates tables itself, so a fresh volume without `migrate` yields 500s.
- `STRIPE_WEBHOOK_SECRET` has **no default** in `app/config.py`, so every app service fails to boot without it. It is supplied as an external secret through the platform file `/run/base44/app.env` (also holds any replacement value). `DATABASE_URL` / `REDIS_URL` point at the local `db` / `redis` services and are set inline in compose (local infra credentials, not user secrets).
- Host port **3000 → api:8000** is the preview entry point; **8000 → api:8000** is the same service for `stripe listen --forward-to localhost:8000/webhook/stripe`. There is **no web UI** — `/` answers `404` (docs are disabled in `app/main.py` on purpose) and `/healthz` is the liveness endpoint. Judge the stack by API responses, not by a page.
- Healthchecks: `api` fetches `/healthz`, `worker` runs `celery inspect ping`, `beat` checks its pidfile (`/tmp/celerybeat.pid`; beat also keeps its schedule in `/tmp` so the repo stays clean).
- Because the entry point answers in JSON (no HTML), the platform's in-page preview bridge cannot attach to it: browser-based preview checks are unavailable for this app. Verify over HTTP instead (`curl`, or `fetch` from a script) and point yourself at `/healthz`, not `/`.
- `app/celery_app.py` passes `include=["app.tasks"]` to `Celery(...)`. Without it, workers start with no registered task and every message fails with `KeyError: 'app.tasks.dispatch_outbox'` (the API only got away with it because `app/main.py` imports `app.tasks`).

## Verify
Unit tests (signature path, mode checks, event filtering, persistence contract):
```bash
docker compose -f docker-compose.base44.yml exec -T api python -m pytest -q
```

End-to-end happy path (verifies rate limiter → signature → inbox+outbox → beat → worker). Sign the body **inside the api container** with the app's own secret and never print the secret:
```bash
docker compose -f docker-compose.base44.yml exec -T api python -c "
import hashlib,hmac,json,os,time,urllib.request
secret=os.environ['STRIPE_WEBHOOK_SECRET']; eid='evt_base44_check'
body={'id':eid,'object':'event','type':'payment_intent.succeeded','livemode':False,'data':{'object':{'id':'pi_base44_check'}}}
raw=json.dumps(body,separators=(',',':')).encode(); ts=int(time.time())
sig='t=%d,v1=%s'%(ts,hmac.new(secret.encode(),('%d.'%ts).encode()+raw,hashlib.sha256).hexdigest())
req=urllib.request.Request('http://localhost:8000/webhook/stripe',data=raw,headers={'Stripe-Signature':sig,'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())"
# then, after ~10s (beat publishes the outbox row, the worker processes it):
docker compose -f docker-compose.base44.yml exec -T db psql -U stripe -d stripe_webhooks -c \
  "select event_id,status,attempts from webhook_events where event_id='evt_base44_check';"
# expected: status = processed. Clean up rows you created yourself.
```
Other checks: unsigned `POST /webhook/stripe` → `400`; `POST /internal/webhook/replay/x` without `X-API-Key` → `401`; Redis down → webhook routes answer `503` (rate limiter fails closed).

## Reset
```bash
docker compose -f docker-compose.base44.yml down -v   # drops pgdata/redisdata
```
