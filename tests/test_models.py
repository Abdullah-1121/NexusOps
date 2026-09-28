"""_enforce_json strict-parsing contract (FR-4 / NFR-4) and the terminal
quota-exhaustion classifier (postmortem guardrail, 2026-09-22)."""

import asyncio

import pytest

import httpx

from app.models import ModelError, _chat, _enforce_json, _quota_exhausted


class _FakeClient:
    """Replaces httpx.AsyncClient for the _chat failure branches without
    touching the network: `post` either raises the injected httpx error or
    returns the injected fake response."""

    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def test_bare_json_parses():
    assert _enforce_json('{"a": 1}', "m") == {"a": 1}


def test_fenced_json_unwrapped_then_strict():
    # GLM via TokenRouter fences JSON even under strict response_format; the
    # fence is transport, unwrapped exactly — the body is still parsed strictly.
    assert _enforce_json('```json\n{"severity": "info", "ok": true}\n```', "m") == {
        "severity": "info",
        "ok": True,
    }


def test_garbage_is_loud():
    with pytest.raises(ModelError, match="unparsable JSON"):
        _enforce_json("not json at all", "m")


def test_fenced_garbage_is_still_loud():
    # fence tolerance must never smuggle in a broken body
    with pytest.raises(ModelError, match="unparsable JSON"):
        _enforce_json("```json\n{\"a\":\n```", "m")


# --- quota-exhaustion classifier (postmortem guardrail, 2026-09-22) -----------
# Treating a terminal daily-cap 429 as retryable is how a congested judge pass
# silently burned the whole 20-call free day: each retry multiplies the burn,
# and the quota does NOT reset inside our retry window (midnight Pacific).


def test_gemini_quota_429_is_terminal():
    # exact Gemini wording observed live on gemini-3.7/3.8-flash
    assert _quota_exhausted(
        '{"error":{"code":429,"message":"You exceeded your current quota, please '
        'check your plan and billing details.","status":"RESOURCE_EXHAUSTED"}}'
    )


def test_openrouter_daily_cap_wording_is_terminal():
    assert _quota_exhausted("free-models-per-day limit reached")


def test_plain_rate_limit_429_is_not_terminal():
    # transient 429 (rate limit, congestion) must stay retryable
    assert not _quota_exhausted("429 too many requests, retry after backoff")


def test_http_503_congestion_is_not_terminal():
    # 503 "high demand" recovers in minutes and must stay retryable
    assert not _quota_exhausted(
        'This model is currently experiencing high demand. Spikes in demand '
        "are usually temporary. Please try again later."
    )


# --- D-15: typed, never-empty model failures (2026-09-28, user-found) ---------
# The live console rendered a bare "model error" because _chat's transport
# branch formatted `f"...: {e}"` where the httpx cause's str() was EMPTY. The
# guardrails: every ModelError carries a `kind` (transport/http/quota/schema/
# empty/config) and every message falls back to the exception class name so
# the operator always learns the class and the direction, never a blank tail.


def test_transport_error_with_empty_str_never_yields_empty_message(monkeypatch):
    """The exact live bug: an httpx cause whose str() == "" must still produce
    a classified, non-empty ModelError — the class name is the fallback."""

    class _EmptyStrTransportError(httpx.TransportError):
        def __str__(self):
            return ""  # precisely the flat cause observed live on the gateway

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _FakeClient(_EmptyStrTransportError(""))
    )
    with pytest.raises(ModelError) as ei:
        asyncio.run(_chat("k", "http://provider", {}, "gemini-x", 1.0, None))
    err = ei.value
    assert err.kind == "transport"
    assert err.retryable is True
    assert "transport" in str(err)
    assert "_EmptyStrTransportError" in str(err)  # class-name fallback carried the detail
    assert str(err).rstrip().endswith(":") is False  # never a bare trailing colon


def test_quota_429_is_kind_quota_and_not_retryable(monkeypatch):
    """Daily-cap 429 must be typed `quota` and TERMINAL (2026-09-22 postmortem:
    retrying a quota that resets on a provider schedule burns the free day)."""
    body = ('{"error":{"code":429,"message":"You exceeded your current quota, please '
            'check your plan and billing details.","status":"RESOURCE_EXHAUSTED"}}')
    resp = _FakeResponse(429, body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))
    with pytest.raises(ModelError) as ei:
        asyncio.run(_chat("k", "http://provider", {}, "gemini-x", 1.0, None))
    err = ei.value
    assert err.kind == "quota"
    assert err.retryable is False
    assert "quota" in str(err)


def test_schema_violation_is_kind_schema():
    with pytest.raises(ModelError) as ei:
        _enforce_json("not json at all", "m")
    assert ei.value.kind == "schema"


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text