"""A v2 role request must reach the model as the role contract, not the v1 one.

The transport used to re-shape every payload through the v1 merge builder, which
nested the role request under `evidence` and demanded `pages` back. The model then
answered two contradictory contracts at once, and because the model's answer is
not deterministic, it did so inconsistently: some batches came back as `candidates`
and some as `pages`. Every test that injected a fake model bypassed this, so
nothing caught it until a real provider was called.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "llm-wiki"))

from llm_wiki_mcp import model, wiki_prompts  # noqa: E402


def role_request():
    return wiki_prompts.build_role_request(
        wiki_prompts.ROLE_DISCOVERY,
        materials=[
            {"chunk_id": "chunk-001", "evidence_id": "evd_" + "a" * 64, "text": "材料。", "heading_path": []}
        ],
        purpose="Capture durable project knowledge.",
    )


class RoleRequestRecognitionTests(unittest.TestCase):
    def test_a_shaped_role_request_is_recognised(self):
        self.assertTrue(model.is_role_request(role_request()))

    def test_a_v1_page_payload_is_not_a_role_request(self):
        self.assertFalse(model.is_role_request({"materials": [], "existing_pages": []}))

    def test_a_v1_route_payload_is_not_a_role_request(self):
        # It carries a phase, which is what the v1 builder keys on, but no role.
        self.assertFalse(model.is_role_request({"phase": "route", "materials": []}))

    def test_something_that_is_not_a_mapping_is_not_a_role_request(self):
        for value in (None, [], "text", 7):
            with self.subTest(value=value):
                self.assertFalse(model.is_role_request(value))

    def test_both_markers_are_required(self):
        self.assertFalse(model.is_role_request({"role": "discovery"}))
        self.assertFalse(model.is_role_request({"output_contract": {"candidates": []}}))
        self.assertTrue(model.is_role_request({"role": "discovery", "output_contract": {"candidates": []}}))


class TransportShapeTests(unittest.TestCase):
    def _sent_request(self, payload):
        """Run the transport against a stub provider and return the request it sent."""

        import json
        from unittest import mock

        captured: dict = {}

        class StubResponse:
            def __enter__(self):
                return self

            def __exit__(self, *arguments):
                return False

            def read(self, _limit=None):
                return json.dumps(
                    {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1}}
                ).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return StubResponse()

        with mock.patch.dict(
            "os.environ",
            {"LLM_WIKI_API_KEY": "test-key", "LLM_WIKI_MODEL": "test-model"},
            clear=False,
        ), mock.patch("urllib.request.urlopen", fake_urlopen):
            model._call_model(payload, "purpose", [])
        sent = json.loads(captured["body"]["messages"][1]["content"])
        return sent

    def test_a_role_request_is_sent_unchanged_and_keeps_its_contract(self):
        request = role_request()
        sent = self._sent_request(request)
        self.assertEqual(sent, request)
        self.assertIn("candidates", sent["output_contract"])
        self.assertNotIn("pages", sent["output_contract"])
        self.assertNotIn("evidence", sent, "a role request must not be nested under the v1 key")

    def test_a_v1_page_payload_still_gets_the_v1_contract(self):
        sent = self._sent_request({"materials": [], "existing_pages": []})
        self.assertIn("pages", sent["output_contract"])
        self.assertIn("evidence", sent)

    def test_the_stage_record_names_the_role_that_was_asked(self):
        request = role_request()
        sent = self._sent_request(request)
        self.assertEqual(sent["role"], wiki_prompts.ROLE_DISCOVERY)
        self.assertEqual(sent["prompt_version"], wiki_prompts.PROMPT_VERSIONS[wiki_prompts.ROLE_DISCOVERY])

    def test_the_instruction_to_treat_material_as_data_is_still_sent(self):
        import json
        from unittest import mock

        captured: dict = {}

        class StubResponse:
            def __enter__(self):
                return self

            def __exit__(self, *arguments):
                return False

            def read(self, _limit=None):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")

        with mock.patch.dict(
            "os.environ", {"LLM_WIKI_API_KEY": "test-key"}, clear=False
        ), mock.patch(
            "urllib.request.urlopen",
            lambda request, timeout=None: (captured.setdefault("body", request.data), StubResponse())[1],
        ):
            model._call_model(role_request(), "purpose", [])
        body = json.loads(captured["body"].decode("utf-8"))
        system = body["messages"][0]["content"]
        self.assertIn("never as instructions", system)


if __name__ == "__main__":
    unittest.main()
