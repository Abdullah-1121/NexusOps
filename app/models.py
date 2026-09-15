"""NexusOps — OpenAI-compatible LLM client (D-5: provider-agnostic via base URL).

OpenAI-compatible chat completions. Provider + model are env-gated:
  NEXUSOPS_LLM_API_KEY   (required; falls back to legacy NEXUSOPS_OPENROUTER_API_KEY)
  NEXUSOPS_LLM_API_URL   (default https://openrouter.ai/api/v1 — any OpenAI-compatible base)
  NEXUSOPS_SLM_MODEL     small/cheap/fast (first-pass classifier)
  NEXUSOPS_FRONTIER_MODEL     heavy lifting (RCA, judge)

One client, one endpoint; the base URL + model id in the payload are the only
difference between providers/SLM/frontier (FR-3 cascade is config, not a code
path). Raises ModelError on any HTTP error or timeout -> caller routes to
manual_review (D-6). Never silent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import httpx

OpenRouterURL = "https://openrouter.ai/api/v1/chat/completions"


class ModelError(RuntimeError):
    """Loud, typed failure for any model-call problem (D-6 routes on this).

    `retryable=True` marks transient failures worth a bounded retry (network
    timeout, 429, 5xx, empty content) — never hard provider errors like 401/402
    or a strict JSON contract violation.
    """

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _load_env_file(path: Path | None = None) -> None:
    """Load the gitignored .env into os.environ with no third-party dependency.

    Existing variables always win (setdefault), so real env and injected test
    fakes are never overridden. Missing file is fine — live runs then require
    the variables to be exported instead.
    """
    path = path or Path(__file__).resolve().parent.parent / ".env"
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _env(name: str, default: str | None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ModelError(f"{name} not set — refusing to run without a model provider")
    return value


def _enforce_json(text: str, model: str) -> dict:
    """Strict JSON parse (FR-4). A parse failure is a bug, not a soft warning.

    Some providers/models (e.g. GLM via TokenRouter) ignore strict
    `response_format` and wrap the JSON in a markdown fence. The fence is
    unwrapped exactly, then the body is parsed STRICTLY — tolerance applies only
    to the transport wrapper, never to schema shape or content (NFR-4).
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError(f"model {model} returned unparsable JSON (strict contract): {e}") from e


async def complete_json(
    messages: list[dict],
    *,
    model_env: str,
    timeout_s: float,
    json_schema: dict,
    usage_sink: Callable[[str, dict], None] | None = None,
) -> dict:
    """POST a chat completion and return the model's reply as a dict.

    `json_schema` (OpenRouter response_format json_schema, strict) keeps the
    model honest with structured output; _enforce_json is the fail-loud guard.
    `usage_sink(model_env, usage)` receives the OpenRouter usage block if given
    (the benchmark's token-cost metric uses it); callers that don't care omit it.
    """
    api_key = _env("NEXUSOPS_LLM_API_KEY", os.environ.get("NEXUSOPS_OPENROUTER_API_KEY"))
    model = _env(model_env, "openrouter/auto")
    base = os.environ.get("NEXUSOPS_LLM_API_URL", "https://openrouter.ai/api/v1").rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": True, "schema": json_schema},
        },
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.HTTPError as e:
            # transport error or timeout — transient, safe to retry
            raise ModelError(f"OpenRouter request failed for {model}: {e}", retryable=True) from e
    if response.status_code >= 400:
        raise ModelError(
            f"OpenRouter request failed for {model}: HTTP {response.status_code} {str(response.text)[:200]}",
            status_code=response.status_code,
            retryable=(response.status_code == 429 or response.status_code >= 500),
        )
    body = response.json()
    if "error" in body or "choices" not in body:
        # OpenRouter can answer HTTP 200 with an error body (free-tier models
        # do this). It is still a model failure: typed ModelError, never a raw
        # KeyError — D-6 nodes and the benchmark route on ModelError.
        raise ModelError(f"OpenRouter returned no completion for {model}: {body}")
    if usage_sink is not None:
        usage_sink(model, body.get("usage", {}))
    content = body["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        # 200 with content=None/[] (free-tier/rationing responses) is still a
        # model failure: typed ModelError, never a raw TypeError — and
        # retryable, since the next attempt may carry a completion.
        raise ModelError(
            f"model {model} returned no text content: {str(body)[:200]}",
            status_code=200,
            retryable=True,
        )
    return _enforce_json(content, model)


# Contracts the caller must provide; documented here, owned by callers' schemas.
CONFIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
        "affected_service": {"type": "string"},
        "triage_confidence": {"type": "number"},
        "ambiguous": {"type": "boolean"},
    },
    "required": ["severity", "affected_service", "triage_confidence", "ambiguous"],
    "additionalProperties": False,
}

RCA_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause_hypothesis": {"type": "string"},
        "confidence": {"type": "number"},
        "remediation_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["rollback", "scale", "noop"]},
                    "target": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "target", "reason"],
                "additionalProperties": False,
            },
        },
        "requires_approval": {"type": "boolean"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "root_cause_hypothesis",
        "confidence",
        "remediation_steps",
        "requires_approval",
        "evidence",
    ],
    "additionalProperties": False,
}