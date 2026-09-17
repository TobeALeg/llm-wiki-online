"""Which model serves which role, and what the provider reported about the call.

`LLM_WIKI_MODEL` stays the compatible default. A role that has its own variable
uses it, and the answer carries where it came from so a report can say which
configuration produced a run without guessing.

Standard library only, so the local CLI reads the same resolver the server does
and the two cannot disagree about which model answered.

Usage is recorded as the provider reported it. A vendor that returns no usage
block is recorded as `unknown`, because filling in an estimate and labelling it a
measurement is worse than admitting the number is missing.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

DEFAULT_MODEL_ENV = "LLM_WIKI_MODEL"
DEFAULT_MODEL = "deepseek-flash"

MODEL_ROLE_ENV: Mapping[str, str] = {
    "discovery": "LLM_WIKI_DISCOVERY_MODEL",
    "reasoning": "LLM_WIKI_REASONING_MODEL",
    "grounding": "LLM_WIKI_GROUNDING_MODEL",
    "render": "LLM_WIKI_RENDER_MODEL",
    "synthesis": "LLM_WIKI_REASONING_MODEL",
    "value": "LLM_WIKI_GROUNDING_MODEL",
    "identity": "LLM_WIKI_REASONING_MODEL",
}
"""Roles a stage may ask for, and the variable that can override each.

Several stage names map to one variable on purpose. The spec offers four
overrides, and the v2 stages that do the same kind of work share one rather than
multiplying configuration nobody asked for.
"""

UNKNOWN_USAGE = "unknown"


def resolve_model(role: str | None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The model id for a role, and the variable it came from."""

    environment = os.environ if environ is None else environ
    variable = MODEL_ROLE_ENV.get(str(role or ""))
    if variable:
        configured = str(environment.get(variable, "")).strip()
        if configured:
            return {"role": str(role), "model": configured, "source": variable}
    fallback = str(environment.get(DEFAULT_MODEL_ENV, "")).strip()
    if fallback:
        return {"role": str(role or "default"), "model": fallback, "source": DEFAULT_MODEL_ENV}
    return {"role": str(role or "default"), "model": DEFAULT_MODEL, "source": "built_in_default"}


def usage_from_response(response: Any) -> dict[str, Any]:
    """What the provider said about this call, or `unknown`.

    Only a field the provider actually returned is reported as a number. A missing
    block, a missing key, and a non-integer value all become `unknown` rather than
    a zero that would read as a measured zero.
    """

    if not isinstance(response, Mapping):
        return {"input_tokens": UNKNOWN_USAGE, "output_tokens": UNKNOWN_USAGE, "source": "no_response_object"}
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return {
            "input_tokens": UNKNOWN_USAGE,
            "output_tokens": UNKNOWN_USAGE,
            "source": "provider_reported_no_usage",
        }
    recorded: dict[str, Any] = {"source": "provider_reported"}
    for field, key in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        value = usage.get(key)
        recorded[field] = int(value) if isinstance(value, int) and not isinstance(value, bool) else UNKNOWN_USAGE
    if recorded["input_tokens"] == UNKNOWN_USAGE and recorded["output_tokens"] == UNKNOWN_USAGE:
        recorded["source"] = "provider_usage_block_incomplete"
    return recorded


def stage_record(
    *,
    role: str | None,
    resolved: Mapping[str, str],
    prompt_version: str,
    attempts: int,
    usage: Mapping[str, Any],
    base_url: str = "",
) -> dict[str, Any]:
    """One stage's provenance, in the shape a cost report reads."""

    return {
        "role": str(role or "default"),
        "model": resolved["model"],
        "model_source": resolved["source"],
        "provider": base_url or UNKNOWN_USAGE,
        "prompt_version": prompt_version,
        "attempts": int(attempts),
        "input_tokens": usage.get("input_tokens", UNKNOWN_USAGE),
        "output_tokens": usage.get("output_tokens", UNKNOWN_USAGE),
        "usage_source": usage.get("source", "provider_reported_no_usage"),
    }
