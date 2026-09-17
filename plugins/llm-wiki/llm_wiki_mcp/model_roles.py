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
import re
from pathlib import Path
from typing import Any, Mapping

DEFAULT_MODEL_ENV = "LLM_WIKI_MODEL"
DEFAULT_MODEL = "deepseek-flash"

ENV_FILE_NAME = "env"
"""The one file a person fills in, kept outside the repository.

It sits beside the local knowledge database under `LLM_WIKI_HOME`, so it cannot
be committed by accident and it travels with the machine rather than with a
checkout. `docs/knowledge-v2/env.example` is the template to copy.
"""

_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

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


def env_file_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the filled-in key file lives."""

    environment = os.environ if environ is None else environ
    home = str(environment.get("LLM_WIKI_HOME", "")).strip() or "~/.llm-wiki"
    return Path(home).expanduser() / ENV_FILE_NAME


def load_env_file(
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    override: bool = False,
) -> dict[str, Any]:
    """Read `KEY=VALUE` lines into this process's environment.

    A variable already present wins, because an export made for one command is an
    instruction about that command while the file is a stored default. Set
    `override` for the opposite.

    Nothing here is required. A missing file is reported and ignored, so the tools
    keep working on a machine that configures its key some other way.
    """

    environment = os.environ if environ is None else environ
    target = Path(path).expanduser() if path is not None else env_file_path(environment)
    summary: dict[str, Any] = {
        "path": str(target),
        "present": target.is_file(),
        "loaded": [],
        "already_set": [],
        "ignored": [],
    }
    if not summary["present"]:
        return summary

    for number, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            # A line that is not a comment and not an assignment is a typo the
            # person cannot see unless it is named.
            summary["ignored"].append({"line": number, "text": line[:80]})
            continue
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name in environment and not override:
            summary["already_set"].append(name)
            continue
        if isinstance(environment, dict):
            environment[name] = value
        else:  # pragma: no cover - a read-only mapping was handed in
            os.environ[name] = value
        summary["loaded"].append(name)
    return summary


def credential_report(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What is configured, without printing the secret.

    A key is confirmed by its length and last four characters, which is enough to
    catch a truncated paste or the wrong environment's key and not enough to be
    worth redacting from a terminal.
    """

    environment = os.environ if environ is None else environ
    chosen = ""
    for name in ("LLM_WIKI_API_KEY", "DEEPSEEK_API_KEY"):
        if str(environment.get(name, "")).strip():
            chosen = name
            break
    key = str(environment.get(chosen, "")).strip() if chosen else ""
    return {
        "key_variable": chosen,
        "key_present": bool(key),
        "key_length": len(key),
        "key_tail": key[-4:] if len(key) >= 4 else "",
        "base_url": str(environment.get("LLM_WIKI_BASE_URL", "https://api.deepseek.com")).rstrip("/"),
        "models": {
            role: resolve_model(role, environment)
            for role in ("discovery", "reasoning", "grounding", "render")
        },
        "env_file": str(env_file_path(environment)),
        "env_file_present": env_file_path(environment).is_file(),
    }


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
