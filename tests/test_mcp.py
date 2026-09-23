"""Feature 2 acceptance tests (spec §5.2): ToolError vs empty; rollback gate; trace."""

from datetime import datetime, timedelta, timezone

import pytest

from app.mcp_server import (
    CALL_LOG,
    _APPROVED,
    _now,
    approve_rollback,
    fetch_service_logs,
    query_prometheus_metrics,
    trigger_github_rollback,
)
from mcp.server.mcpserver.exceptions import ToolError

WINDOW = lambda d=0: {  # noqa: E731
    "start": (_now() - timedelta(hours=2) - timedelta(minutes=d)).isoformat(),
    "end": (_now() - timedelta(minutes=5 + d)).isoformat(),
}


@pytest.fixture(autouse=True)
def reset_state():
    _APPROVED.clear()
    CALL_LOG.clear()


def test_fetch_logs_known_service_returns_logs():
    result = fetch_service_logs("auth", WINDOW())
    assert isinstance(result["logs"], list) and len(result["logs"]) > 0
    first = result["logs"][0]
    assert {"timestamp", "level", "message"} <= set(first)


def test_fetch_logs_empty_window_is_empty_not_error():
    result = fetch_service_logs("catalog", WINDOW())
    assert result == {"logs": []}


def test_fetch_logs_unknown_service_is_structured_error():
    with pytest.raises(ToolError, match="does not exist"):
        fetch_service_logs("ghost-service", WINDOW())


def test_empty_vs_unknown_are_distinguishable():
    empty = fetch_service_logs("catalog", WINDOW())
    with pytest.raises(ToolError):
        fetch_service_logs("ghost-service", WINDOW())
    assert empty["logs"] == []
    assert "ghost-service" not in {l["message"] for l in empty["logs"]}


def test_metrics_known_and_unknown():
    series = query_prometheus_metrics("error_rate", "30m")
    assert {"timestamp", "value"} <= set(series["series"][0])
    with pytest.raises(ToolError, match="does not exist"):
        query_prometheus_metrics("page_views_total", "30m")


def test_mock_data_is_order_independent_and_repeatable():
    first = fetch_service_logs("auth", WINDOW())
    query_prometheus_metrics("error_rate", "30m")  # interleave another call
    second = fetch_service_logs("auth", WINDOW())
    assert first == second


def test_rollback_gate_blocks_before_approval(monkeypatch):
    """NG-1: no approval ⇒ rejected, and NO HTTP call is ever made."""
    calls: list = []

    def fake_request(method, path, **kw):
        calls.append((method, path))
        raise AssertionError("gate block must never reach the network")

    monkeypatch.setattr("app.rollback._request", fake_request)
    result = trigger_github_rollback("abc123")
    assert result["status"] == "rejected"
    assert calls == []


def test_rollback_performed_after_approval(monkeypatch):
    """Approved ⇒ real scoped rollback: tag created + verified (D-10)."""
    created: list = []

    def fake_request(method, path, **kw):
        if method == "POST" and path.endswith("/git/refs"):
            created.append(path)
            return 201, {"ref": f"refs/tags/{kw['body']['ref'].split('/')[-1]}", "object": {"sha": "t0"}}
        if method == "GET" and "/git/ref/tags/" in path:
            return 200, {"ref": path, "object": {"sha": "t0"}}
        if path == "/repos/owner/demo":
            return 200, {"default_branch": "main"}
        if path == "/repos/owner/demo/git/ref/heads/main":
            return 200, {"object": {"sha": "t0"}}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr("app.rollback._request", fake_request)
    monkeypatch.setenv("NEXUSOPS_GITHUB_REPO", "owner/demo")
    monkeypatch.setenv("NEXUSOPS_GITHUB_TOKEN", "tok")
    approve_rollback("abc123")
    result = trigger_github_rollback("abc123")
    assert result["status"] == "performed"
    assert result["verified"] is True
    assert created, "the real rollback must make a tag-create call"


def test_every_call_is_trace_visible():
    fetch_service_logs("auth", WINDOW())
    query_prometheus_metrics("error_rate", "30m")
    trigger_github_rollback("abc123")
    assert [c["tool"] for c in CALL_LOG] == [
        "fetch_service_logs",
        "query_prometheus_metrics",
        "trigger_github_rollback",
    ]


def test_benchmark_fixture_services_are_known_to_evidence_store():
    """Contract guard (live-run finding): F5 fixtures and the F2 synthetic store
    must agree on service names, or live evidence calls loud-fail for every
    incident and starve the RCA. Silence ≠ nonexistence for the drift services."""
    from app.benchmark import build_fixtures

    for f in build_fixtures():
        service = f.alert["service"]
        if service in {"ghost", "orphan", "dangling"}:
            assert fetch_service_logs(service, WINDOW()) == {"logs": []}
        else:
            assert isinstance(fetch_service_logs(service, WINDOW())["logs"], list)