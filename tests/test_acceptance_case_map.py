"""Every acceptance case in the spec is proven by a real test, or this fails.

The spec ships 56 named cases. "All cases pass" is only meaningful if each one
resolves to a test that can fail, so this walks the discovered suite, reads the
case ids each test declares through `acceptance.case`, and compares the two sets.
An id with no test fails. An id no longer in the spec fails. Neither can be
papered over by editing this file, because the expected set is read from the
spec's own JSON.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import acceptance  # noqa: E402


class AcceptanceCaseMapTests(unittest.TestCase):
    def setUp(self):
        self.spec = acceptance.spec_cases()
        self.covered = acceptance.covered_cases()

    def test_the_spec_ships_all_fifty_six_cases(self):
        self.assertEqual(len(self.spec), 56)
        self.assertEqual(len(self.spec), len(set(self.spec)))

    def test_every_case_id_is_proven_by_at_least_one_test(self):
        uncovered = sorted(case_id for case_id in self.spec if case_id not in self.covered)
        self.assertEqual(
            uncovered,
            [],
            "these acceptance cases have no test declaring them; add @case(...) "
            "to the test that proves each one",
        )

    def test_no_test_claims_a_case_the_spec_does_not_define(self):
        unknown = sorted(case_id for case_id in self.covered if case_id not in self.spec)
        self.assertEqual(unknown, [], "these case ids are not in the spec")

    def test_the_case_map_report_is_written_next_to_the_evals(self):
        """The release checklist reads a report, so the report is generated here.

        Writing it from the same walk that the assertions use means the committed
        map cannot claim coverage the suite does not have.
        """

        report = acceptance.case_map()
        self.assertEqual(report["uncovered"], [])
        self.assertEqual(report["unknown"], [])
        self.assertEqual(report["covered_count"], report["spec_count"])
        destination = acceptance.CASE_FILE.parent / "case_map.json"
        destination.write_text(
            json.dumps(
                {
                    "spec_count": report["spec_count"],
                    "covered_count": report["covered_count"],
                    "uncovered": report["uncovered"],
                    "unknown": report["unknown"],
                    "map": report["map"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
