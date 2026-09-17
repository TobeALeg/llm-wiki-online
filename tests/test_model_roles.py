"""Role-based model selection and provider-reported usage.

The spec asks for optional per-role model overrides over a compatible default,
and for per-stage recording of which model answered and what it cost. A vendor
that reports no usage must be recorded as unknown rather than as an estimate that
later reads like a measurement.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp.model_roles import (  # noqa: E402
    DEFAULT_MODEL,
    MODEL_ROLE_ENV,
    UNKNOWN_USAGE,
    resolve_model,
    stage_record,
    usage_from_response,
)


class ResolveModelTests(unittest.TestCase):
    def test_the_default_model_variable_serves_every_role_when_nothing_else_is_set(self):
        environment = {"LLM_WIKI_MODEL": "base-model"}
        for role in ("discovery", "reasoning", "grounding", "render", "value", "identity", "synthesis"):
            with self.subTest(role=role):
                resolved = resolve_model(role, environment)
                self.assertEqual(resolved["model"], "base-model")
                self.assertEqual(resolved["source"], "LLM_WIKI_MODEL")

    def test_a_role_variable_overrides_the_default_for_that_role_only(self):
        environment = {
            "LLM_WIKI_MODEL": "base-model",
            "LLM_WIKI_DISCOVERY_MODEL": "cheap-model",
        }
        discovery = resolve_model("discovery", environment)
        reasoning = resolve_model("reasoning", environment)
        self.assertEqual(discovery["model"], "cheap-model")
        self.assertEqual(discovery["source"], "LLM_WIKI_DISCOVERY_MODEL")
        self.assertEqual(reasoning["model"], "base-model")
        self.assertEqual(reasoning["source"], "LLM_WIKI_MODEL")

    def test_every_documented_role_variable_is_wired(self):
        documented = {
            "LLM_WIKI_DISCOVERY_MODEL",
            "LLM_WIKI_REASONING_MODEL",
            "LLM_WIKI_GROUNDING_MODEL",
            "LLM_WIKI_RENDER_MODEL",
        }
        self.assertTrue(documented <= set(MODEL_ROLE_ENV.values()))
        for variable in documented:
            role = next(name for name, value in MODEL_ROLE_ENV.items() if value == variable)
            with self.subTest(variable=variable):
                self.assertEqual(
                    resolve_model(role, {variable: "x"})["source"],
                    variable,
                )

    def test_an_empty_override_falls_through_to_the_default(self):
        resolved = resolve_model("discovery", {"LLM_WIKI_DISCOVERY_MODEL": "   ", "LLM_WIKI_MODEL": "base"})
        self.assertEqual(resolved["model"], "base")
        self.assertEqual(resolved["source"], "LLM_WIKI_MODEL")

    def test_nothing_configured_reports_the_built_in_default(self):
        resolved = resolve_model("discovery", {})
        self.assertEqual(resolved["model"], DEFAULT_MODEL)
        self.assertEqual(resolved["source"], "built_in_default")


class UsageTests(unittest.TestCase):
    def test_a_provider_that_reports_nothing_is_recorded_as_unknown(self):
        usage = usage_from_response({"choices": [{"message": {"content": "{}"}}]})
        self.assertEqual(usage["input_tokens"], UNKNOWN_USAGE)
        self.assertEqual(usage["output_tokens"], UNKNOWN_USAGE)
        self.assertEqual(usage["source"], "provider_reported_no_usage")

    def test_a_partial_usage_block_keeps_the_missing_field_unknown(self):
        usage = usage_from_response({"usage": {"prompt_tokens": 120}})
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["output_tokens"], UNKNOWN_USAGE)

    def test_a_complete_usage_block_is_reported_verbatim(self):
        usage = usage_from_response({"usage": {"prompt_tokens": 120, "completion_tokens": 40}})
        self.assertEqual(usage, {
            "source": "provider_reported",
            "input_tokens": 120,
            "output_tokens": 40,
        })

    def test_a_boolean_is_not_accepted_as_a_count(self):
        usage = usage_from_response({"usage": {"prompt_tokens": True, "completion_tokens": 5}})
        self.assertEqual(usage["input_tokens"], UNKNOWN_USAGE)
        self.assertEqual(usage["output_tokens"], 5)

    def test_a_non_object_response_does_not_invent_numbers(self):
        for value in (None, [], "text", 7):
            with self.subTest(value=value):
                usage = usage_from_response(value)
                self.assertEqual(usage["input_tokens"], UNKNOWN_USAGE)
                self.assertEqual(usage["output_tokens"], UNKNOWN_USAGE)


class StageRecordTests(unittest.TestCase):
    def test_a_stage_record_names_the_model_the_prompt_and_the_attempts(self):
        record = stage_record(
            role="discovery",
            resolved=resolve_model("discovery", {"LLM_WIKI_DISCOVERY_MODEL": "cheap"}),
            prompt_version="2.0.0",
            attempts=2,
            usage=usage_from_response({"usage": {"prompt_tokens": 5, "completion_tokens": 2}}),
            base_url="https://api.example",
        )
        self.assertEqual(record["role"], "discovery")
        self.assertEqual(record["model"], "cheap")
        self.assertEqual(record["model_source"], "LLM_WIKI_DISCOVERY_MODEL")
        self.assertEqual(record["prompt_version"], "2.0.0")
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(record["input_tokens"], 5)
        self.assertEqual(record["provider"], "https://api.example")

    def test_an_unreported_provider_is_named_unknown_not_blank(self):
        record = stage_record(
            role="grounding",
            resolved=resolve_model("grounding", {}),
            prompt_version="2.0.0",
            attempts=1,
            usage=usage_from_response({}),
        )
        self.assertEqual(record["provider"], UNKNOWN_USAGE)
        self.assertEqual(record["input_tokens"], UNKNOWN_USAGE)
        self.assertEqual(record["usage_source"], "provider_reported_no_usage")


if __name__ == "__main__":
    unittest.main()
