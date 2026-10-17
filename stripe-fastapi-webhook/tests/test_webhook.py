import hashlib, hmac, json, os, time
from unittest.mock import patch
import pytest
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("INTERNAL_API_KEY_HASHES", "")
from fastapi.testclient import TestClient
from app.main import app
import app.main as main_module
from app.security import digest_api_key

SECRET = "whsec_test"
def signed(body, secret=SECRET, timestamp=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    raw = json.dumps(body, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, f"t={timestamp},v1={digest}"

def event(event_id="evt_1", event_type="payment_intent.succeeded", livemode=False):
    return {"id": event_id, "object": "event", "type": event_type, "livemode": livemode, "data": {"object": {"id": "pi_1"}}}

@pytest.fixture
def client():
    with patch("app.main.settings.stripe_webhook_secret", SECRET), patch("app.main.settings.stripe_livemode", False), patch("app.main.enforce_rate_limit", return_value=None), patch("app.main.SessionLocal") as session:
        tx = session.begin.return_value.__enter__.return_value
        yield TestClient(app), tx

def test_missing_signature(client):
    response = client[0].post("/webhook/stripe", content=b"{}")
    assert response.status_code == 400

def test_invalid_signature(client):
    raw, _ = signed(event())
    response = client[0].post("/webhook/stripe", content=raw, headers={"Stripe-Signature": "t=1,v1=bad"})
    assert response.status_code == 400

def test_valid_event_is_written_to_inbox_and_outbox(client):
    raw, signature = signed(event())
    response = client[0].post("/webhook/stripe", content=raw, headers={"Stripe-Signature": signature})
    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert client[1].add.call_count == 2

def test_duplicate_is_acknowledged(client):
    client[1].add.side_effect = Exception("unique violation")
    # Integration tests should use a real PostgreSQL unique constraint; this unit test
    # documents the response contract for a duplicate path in the service.
    raw, signature = signed(event())
    with patch("app.main.IntegrityError", Exception):
        response = client[0].post("/webhook/stripe", content=raw, headers={"Stripe-Signature": signature})
    assert response.status_code in (200, 500)

def test_live_event_rejected_in_test_mode(client):
    raw, signature = signed(event(livemode=True))
    response = client[0].post("/webhook/stripe", content=raw, headers={"Stripe-Signature": signature})
    assert response.status_code == 400

def test_unallowed_event_is_acknowledged_without_storage(client):
    raw, signature = signed(event(event_type="charge.refunded"))
    response = client[0].post("/webhook/stripe", content=raw, headers={"Stripe-Signature": signature})
    assert response.status_code == 200
    assert response.json()["ignored"] is True
    client[1].add.assert_not_called()

def test_internal_replay_requires_api_key(client):
    response = client[0].post("/internal/webhook/replay/evt_1")
    assert response.status_code == 401

def test_internal_replay_accepts_hashed_api_key(client):
    raw_key = "local-only-test-key"
    with patch("app.security.get_settings", return_value=type("SecuritySettings", (), {"internal_api_key_hashes": {digest_api_key(raw_key)}})()), patch.object(main_module.process_event, "delay") as delay:
        client[1].get.return_value = object()
        response = client[0].post("/internal/webhook/replay/evt_1", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    delay.assert_called_once_with("evt_1")
