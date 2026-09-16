"""Session-wide OTel provider so tracing assertions work regardless of test order.

`set_tracer_provider` is one-shot in OTel Python: whoever installs first wins
the global for the whole session. Installing here (imported before any test
module) pins the shared in-memory exporter to every complete_json call in the
suite, and the autouse fixture clears it before each test.

Context7 VERIFIED (open-telemetry/opentelemetry-python, 2026-09-16):
TracerProvider + SimpleSpanProcessor(InMemorySpanExporter) capture finished
spans synchronously.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.tracing as tracing

SESSION_EXPORTER = InMemorySpanExporter()
_SESSION_PROVIDER = TracerProvider()
_SESSION_PROVIDER.add_span_processor(SimpleSpanProcessor(SESSION_EXPORTER))
trace.set_tracer_provider(_SESSION_PROVIDER)
tracing._configured = True


@pytest.fixture(autouse=True)
def _otel_shared_exporter():
    SESSION_EXPORTER.clear()
    yield SESSION_EXPORTER