"""End to end ingest tests: the real CLI as a subprocess against a stub model server.

The unit tests in test_lw_ingest.py drive the module in process. These drive the
shipped artifact the way a user does, over HTTP, so they cover process startup, the
provider environment variables, batching and the on-disk recovery manifest together.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "skills" / "lw" / "scripts" / "wiki.py"
SOURCE_ID = re.compile(r"(?:file|episode):[^\s\"'\\,]{1,200}")


def collect(value, key):
    """Every string stored under `key`, anywhere in a nested JSON document."""

    found = []
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key and isinstance(item, str):
                found.append(item)
            found.extend(collect(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(collect(item, key))
    return found


PROMPT_PREFIX = "Produce the JSON wiki update from this data:\n"


def prompt_payload(body):
    """The structured request the CLI embedded in the user message, or None."""

    for message in body.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, str) and content.startswith(PROMPT_PREFIX):
            try:
                return json.loads(content[len(PROMPT_PREFIX):])
            except json.JSONDecodeError:
                return None
    return None


class StubModel:
    """An OpenAI-compatible chat endpoint that records what the CLI sent."""

    def __init__(self, fail_on=None):
        self.requests = []
        self.answered = []
        self.fail_on = fail_on
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                stub.requests.append(body)
                if stub.fail_on is not None and len(stub.requests) == stub.fail_on:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error": "stub failure"}')
                    return
                stub.answered.append(body)
                prompt = body["messages"][-1]["content"]
                sources = sorted(set(SOURCE_ID.findall(prompt)))
                pages = []
                if sources:
                    pages.append(
                        {
                            "slug": "delivery-plan",
                            "title": "Delivery plan",
                            "type": "decision",
                            "status": "current",
                            "tags": ["delivery"],
                            "summary": f"Recorded from {len(sources)} pieces of evidence.",
                            "body": "Consolidated delivery facts.",
                            "sources": sources,
                        }
                    )
                content = json.dumps({"pages": pages, "note": "stub update"})
                payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        return Handler

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def chunk_ids(self):
        """Chunk identifiers the CLI sent in requests this stub answered."""

        return [
            value
            for body in self.answered
            for value in collect(prompt_payload(body) or {}, "chunk_id")
        ]


class IngestEndToEndTests(unittest.TestCase):
    paragraphs = 30
    width = 1_000

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(
            [sys.executable, str(SCRIPT), "init", "--root", str(self.root)],
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def document(self, name, number):
        blocks = [
            f"{name} paragraph {index}: " + f"word{index} " * (self.width // 8)
            for index in range(self.paragraphs)
        ]
        blocks.append(f"Tail of {name}: the delivery date for {name} is 2026-09-{number:02d}.")
        return "\n\n".join(blocks) + "\n"

    def write_documents(self, count):
        for number in range(1, count + 1):
            name = f"notes-{number}.md"
            (self.root / name).write_text(self.document(name, number), encoding="utf-8")

    def run_cli(self, stub, *arguments):
        environment = dict(os.environ)
        environment.update(
            {
                "LLM_WIKI_API_KEY": "stub-key",
                "LLM_WIKI_BASE_URL": stub.base_url,
                "LLM_WIKI_MODEL": "stub-model",
            }
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT), "update", "--root", str(self.root), *arguments],
            text=True,
            capture_output=True,
            env=environment,
            timeout=120,
        )

    def state(self):
        return json.loads((self.root / ".llm-wiki" / "state.json").read_text(encoding="utf-8"))

    def page_names(self):
        return sorted(path.name for path in (self.root / ".llm-wiki" / "pages").glob("*.md"))

    def manifests(self):
        return sorted((self.root / ".llm-wiki" / "runs").glob("*.json"))

    def chunk_ids_done(self):
        return [
            chunk_id
            for record in self.state()["files"].values()
            for chunk_id in record["chunks_done"]
        ]

    def test_the_tail_of_a_long_document_reaches_the_model(self):
        self.write_documents(1)
        stub = StubModel()
        self.addCleanup(stub.stop)

        result = self.run_cli(stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.dumps(stub.answered, ensure_ascii=False)
        self.assertIn("the delivery date for notes-1.md is 2026-09-01", sent)
        self.assertEqual(self.state()["files"]["notes-1.md"]["status"], "complete")
        self.assertEqual(self.page_names(), ["delivery-plan.md"])

    def test_a_source_set_larger_than_the_request_budget_completes_across_batches(self):
        self.write_documents(8)
        total = sum(
            len((self.root / f"notes-{number}.md").read_text(encoding="utf-8"))
            for number in range(1, 9)
        )
        self.assertGreater(total, 180_000)
        stub = StubModel()
        self.addCleanup(stub.stop)

        result = self.run_cli(stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(len(stub.requests), 1, "the material cannot fit in one request")
        sent = json.dumps(stub.answered, ensure_ascii=False)
        for number in range(1, 9):
            self.assertIn(f"the delivery date for notes-{number}.md is 2026-09-{number:02d}", sent)
        files = self.state()["files"]
        self.assertEqual(
            {name: record["status"] for name, record in files.items()},
            {f"notes-{number}.md": "complete" for number in range(1, 9)},
        )
        for record in files.values():
            self.assertEqual(len(record["chunks_done"]), record["chunks_total"])
            self.assertGreater(record["chunks_total"], 0)
        self.assertEqual(len(set(self.chunk_ids_done())), len(self.chunk_ids_done()))

    def test_a_second_run_with_no_changes_calls_no_model(self):
        self.write_documents(1)
        stub = StubModel()
        self.addCleanup(stub.stop)
        self.assertEqual(self.run_cli(stub).returncode, 0)
        before = len(stub.requests)

        again = self.run_cli(stub)

        self.assertEqual(again.returncode, 0)
        self.assertEqual(len(stub.requests), before)
        self.assertIn("already current", again.stdout)

    def test_a_mid_run_failure_keeps_a_manifest_and_the_retry_finishes_the_job(self):
        self.write_documents(8)
        failing = StubModel(fail_on=2)
        self.addCleanup(failing.stop)

        failed = self.run_cli(failing)

        self.assertNotEqual(failed.returncode, 0, "a 500 from the provider must fail the run")
        self.assertEqual(self.page_names(), [], "no page is committed by a failed run")
        self.assertEqual(len(self.manifests()), 1, "the recovery manifest survives the failure")
        self.assertEqual(self.state()["files"], {}, "a failed run marks no source complete")
        finished = failing.chunk_ids()
        self.assertTrue(finished, "the first batch was answered before the failure")

        healthy = StubModel()
        self.addCleanup(healthy.stop)
        retried = self.run_cli(healthy)

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.manifests(), [])
        self.assertEqual(self.page_names(), ["delivery-plan.md"])
        resubmitted = healthy.chunk_ids()
        self.assertFalse(set(finished) & set(resubmitted), "finished chunks are reused, not redone")
        self.assertTrue(resubmitted)
        self.assertEqual({record["status"] for record in self.state()["files"].values()}, {"complete"})
        self.assertEqual(
            sorted(self.chunk_ids_done()),
            sorted(finished + resubmitted),
            "every chunk is accounted for exactly once",
        )
        committed = (
            self.root / ".llm-wiki" / "pages" / "delivery-plan.md"
        ).read_text(encoding="utf-8")
        for number in range(1, 9):
            self.assertIn(
                f"file:notes-{number}.md@sha256:",
                committed,
                f"the retry dropped the evidence of notes-{number}.md",
            )


if __name__ == "__main__":
    unittest.main()
