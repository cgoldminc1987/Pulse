# Deployment notes

The sample initializes tables with SQLAlchemy `create_all()` for a fresh local stack. Before production, introduce Alembic and commit a reviewed initial migration for `webhook_events` and `webhook_outbox`; run migrations as a one-shot release job before starting API and workers.

Recommended production topology:

- Multiple FastAPI replicas behind an HTTPS load balancer.
- PostgreSQL with backups and a connection pool sized for all API and worker processes.
- Redis configured as a durable, authenticated broker with TLS where supported.
- Multiple Celery workers with `--concurrency` matched to downstream capacity.
- One Celery Beat scheduler, or a managed scheduler, to run the outbox dispatcher.
- Metrics and alerts for HTTP 4xx/5xx, webhook age, queued/processing/failed counts, Celery retries, and Stripe delivery failures.

Rotate the webhook signing secret by accepting the old and new secret during a controlled overlap window, then remove the old secret. Stripe documents endpoint-specific secrets; CLI forwarding secrets and Dashboard endpoint secrets are not interchangeable.
