"""Feature 1 acceptance tests (FR-1, NFR-6). Requires a local Redis (NFR-6)."""

from __future__ import annotations

import uuid

import pytest
import redis as redis_sync
from fastapi.testclient import TestClient

from app.ingest import app

TEST_REDIS_URL = "redis://localhost:6379/15"  # dedicated test DB, away from db 0


@pytest.fixture(autouse=True)
def clean_test_db(monkeypatch):
    # Root isolation fix: tests share no state. Dedicated DB, flushed per test,
    # so the "seen" set from one test can never leak into another (or db 0).
    monkeypatch.setenv("NEXUSOPS_REDIS_URL", TEST_REDIS_URL)
    r = redis_sync.from_url(TEST_REDIS_URL)
    r.flushdb()
    r.close()

VALID_ALERT = {
    "incident_id": str(uuid.uuid4()),
    "occurred_at": "2026-09-08T10:00:00Z",
    "source": "pagerduty",
    "service": "api-gateway",
    "status_code": 500,
    "message": "500s on /users",
    "severity_hint": "critical",
}


def post(client: TestClient, payload: dict) -> dict:
    resp = client.post("/webhook/incident", json=payload)
    return {"status": resp.status_code, "body": resp.json()}


def test_valid_alert_accepted():
    with TestClient(app) as client:
        result = post(client, VALID_ALERT)
    assert result["status"] == 202
    assert result["body"]["incident_id"] == VALID_ALERT["incident_id"]
    assert result["body"]["duplicate"] is False


def test_malformed_alert_rejected_422():
    with TestClient(app) as client:
        result = post(client, {"message": "missing everything else"})
    assert result["status"] == 422
    assert isinstance(result["body"]["detail"], list)  # structured, not silent


def test_unknown_field_rejected_422():
    payload = {**VALID_ALERT, "unexpected_field": "x"}
    with TestClient(app) as client:
        result = post(client, payload)
    assert result["status"] == 422  # extra="forbid" (contract §5.1)


def test_duplicate_delivery_no_double_work():
    with TestClient(app) as client:
        first = post(client, VALID_ALERT)
        second = post(client, VALID_ALERT)
    assert first["body"]["duplicate"] is False
    assert second["body"]["duplicate"] is True
    assert second["status"] == 202  # acknowledged as known, not re-triaged


def test_redis_down_returns_503(monkeypatch):
    monkeypatch.setenv("NEXUSOPS_REDIS_URL", "redis://localhost:6399/0")
    with TestClient(app) as client:
        result = post(client, VALID_ALERT)
    assert result["status"] == 503  # NFR-6: refuse loudly, no degraded mode