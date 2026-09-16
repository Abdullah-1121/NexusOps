"""NexusOps — industry-standard OpenTelemetry tracing for every LLM call.

The choke point is `complete_json` (app/models.py): it opens a `gen_ai.*`
semantic-convention span per model call, so SLM classify, frontier RCA/plan,
and the LLM judge are all traced with one piece of plumbing. Exports over
OTLP using the standard `OTEL_*` environment contract when a collector exists,
otherwise falls back to the console exporter so the harness still runs with
zero infrastructure. Idempotent: the first `get_tracer()` call wires the
provider; tests inject an `InMemorySpanExporter` before any call.

Context7 VERIFIED (open-telemetry/opentelemetry-python, 2026-09-16):
TracerProvider + BatchSpanProcessor(OTLPSpanExporter); OTEL_EXPORTER_OTLP_ENDPOINT
respected; GenAI semantic-convention attribute names (gen_ai.system,
gen_ai.request.model, gen_ai.usage.input_(tokens|output_tokens), gen_ai.response.id).
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter

# GenAI semantic-convention attribute names (stable across spec stabilization;
# the python semconv package marks the gen_ai module _incubating, so the names
# are pinned here against the spec, not against a churning import).
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_ID = "gen_ai.response.id"

TRACER_NAME = "nexusops.llm"

_configured = False


def _default_exporter() -> SpanExporter:
    """OTLP HTTP exporter when OTEL_EXPORTER_OTLP_ENDPOINT is set (standard env
    contract), else console. The endpoint env is the whole industry-standard knob;
    an empty provider config stays visible, not silent."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint)
    # Console spans go to stderr, never stdout: the benchmark CLI owns stdout for
    # its JSON report, and interleaved span dumps corrupt it (a real bug caught
    # in the judge run of 2026-09-16). stderr keeps zero-infra visibility.
    return ConsoleSpanExporter(out=sys.stderr)


def init_tracing(exporter: SpanExporter | None = None, service_name: str | None = None) -> None:
    """Idempotent provider setup. Tests pass an exporter directly (e.g. an
    InMemorySpanExporter) to assert on recorded spans."""
    global _configured
    if _configured:
        return
    resource = Resource.create(
        {"service.name": service_name or os.environ.get("OTEL_SERVICE_NAME", "nexusops")}
    )
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)
    provider.add_span_processor(BatchSpanProcessor(exporter or _default_exporter()))
    _configured = True


def get_tracer():
    """Lazy init: the first call wires the provider from the environment, so an
    entrypoint needs no setup line and tests override before any model call."""
    init_tracing()
    return trace.get_tracer(TRACER_NAME)


def llm_system(base_url: str) -> str:
    """gen_ai.system from the configured base URL (provider host), honest even
    when the base URL is a gateway rather than the model vendor itself."""
    host = urlparse(base_url).hostname
    return host or "openai-compatible"