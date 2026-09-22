"""_enforce_json strict-parsing contract (FR-4 / NFR-4) and the terminal
quota-exhaustion classifier (postmortem guardrail, 2026-09-22)."""

import pytest

from app.models import ModelError, _enforce_json, _quota_exhausted


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