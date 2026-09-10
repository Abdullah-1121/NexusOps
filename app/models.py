"""NexusOps — OpenRouter LLM client (D-5 revised: open-source models via API).

OpenAI-compatible chat completions. Env-gated:
  NEXUSOPS_OPENROUTER_API_KEY     (required)
  NEXUSOPS_SLM_MODEL              default "openrouter/auto" — small/cheap/fast
  NEXUSOPS_FRONTIER_MODEL         default "anthropic/claude-3.5-sonnet" — heavy lifting

One client, one endpoint; the model id in the payload is the only difference
between SLM and frontier calls (FR-3 cascade is a config change, not a code
path). Raises ModelError on any HTTP error or timeout -> caller routes to
manual_review (D-6). Never silent.
"""

from __future__ import annotations

import json
import os

import httpx

OpenRouterURL = "https://openrouter.ai/api/v1/chat/completions"


class ModelError(RuntimeError):
    """Loud, typed failure for any model-call problem (D-6 routes on this)."""


def _env(name: str, default: str | None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ModelError(f"{name} not set — refusing to run without a model provider")
    return value


def _enforce_json(text: str, model: str) -> dict:
    """Strict JSON parse (FR-4). A parse failure is a bug, not a soft warning.

    OpenRouter is called with response_format json_schema + strict=True, so the
    model contractually returns raw JSON. We enforce it: no fence-stripping, no
    "close enough" tolerance — the schema is the contract and a violation fails
    loud (NFR-4). Permissive parsing would hide a model that broke the contract.
    """
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
) -> dict:
    """POST a chat completion and return the model's reply as a dict.

    `json_schema` (OpenRouter response_format json_schema, strict) keeps the
    model honest with structured output; _enforce_json is the fail-loud guard.
    """
    api_key = _env("NEXUSOPS_OPENROUTER_API_KEY", None)
    model = _env(model_env, "openrouter/auto")
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
                OpenRouterURL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise ModelError(f"OpenRouter request failed for {model}: {e}") from e
    content = response.json()["choices"][0]["message"]["content"]
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