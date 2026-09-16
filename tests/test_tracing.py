"""OTel instrumentation tests: every LLM call emits a gen_ai.* span; the judge
rides the same trace. The transport is fake — no network. Spans landed in the
session InMemorySpanExporter provided by conftest (cleared before each test)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from opentelemetry import trace

import app.tracing as tracing
from app.models import ModelError, complete_json

SCHEMA = {
    "type": "object",
    "properties": {"severity": {"type": "string"}, "ok": {"type": "boolean"}},
    "required": ["severity", "ok"],
    "additionalProperties": False,
}


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeClient:
    _resp = _FakeResponse({"error": {"message": "uninitialized"}})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return self._resp


@pytest.fixture(autouse=True)
def _ns_env(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("NEXUSOPS_LLM_API_KEY", "test-key")
    monkeypatch.setenv("NEXUSOPS_LLM_API_URL", "https://provider.example/v1")
    monkeypatch.setenv("NEXUSOPS_SLM_MODEL", "test/slm")


def _call(model_env="NEXUSOPS_SLM_MODEL", schema=SCHEMA):
    return complete_json([{"role": "user", "content": "hello"}], model_env=model_env,
                         timeout_s=5.0, json_schema=schema)


def test_complete_json_emits_gen_ai_span(_otel_shared_exporter):
    _FakeClient._resp = _FakeResponse({
        "id": "cmpl-x",
        "model": "test/slm",
        "choices": [{"message": {"content": '{"severity": "critical", "ok": true}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    out = asyncio.run(_call())
    assert out == {"severity": "critical", "ok": True}
    spans = _otel_shared_exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "llm.chat.completions"
    assert s.attributes[tracing.GEN_AI_SYSTEM] == "provider.example"
    assert s.attributes[tracing.GEN_AI_REQUEST_MODEL] == "test/slm"
    assert s.attributes[tracing.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert s.attributes[tracing.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
    assert s.attributes[tracing.GEN_AI_RESPONSE_ID] == "cmpl-x"
    assert s.status.is_ok


def test_complete_json_error_sets_error_span(_otel_shared_exporter):
    _FakeClient._resp = _FakeResponse({"error": {"message": "no balance"}}, status=402)
    with pytest.raises(ModelError):
        asyncio.run(_call())
    spans = _otel_shared_exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.status.status_code == trace.StatusCode.ERROR
    assert s.attributes["error.type"] == "ModelError"
    # OTel auto-records the propagated exception; our record_exception adds one too.
    assert any(e.name == "exception" for e in s.events)


def test_model_span_nests_under_incident_trace(_otel_shared_exporter):
    _FakeClient._resp = _FakeResponse({
        "id": "cmpl-y",
        "model": "test/slm",
        "choices": [{"message": {"content": '{"severity": "warning", "ok": false}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    async def run():
        with tracing.get_tracer().start_as_current_span(
            "nexusops.incident", attributes={"app.incident.id": "c-01"}
        ):
            await _call()
    asyncio.run(run())
    spans = {s.name: s for s in _otel_shared_exporter.get_finished_spans()}
    incident = spans["nexusops.incident"]
    model_span = spans["llm.chat.completions"]
    assert incident.context.span_id == model_span.parent.span_id
    assert incident.attributes["app.incident.id"] == "c-01"