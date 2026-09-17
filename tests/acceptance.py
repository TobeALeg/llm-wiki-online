"""Declare which acceptance cases a test method discharges.

The spec's 56 cases are the contract. A test states the ids it proves at the test
site, and `test_acceptance_case_map.py` fails when any id is unclaimed. That way
the map cannot drift from the tests: adding a case to the spec without a test, or
deleting a test that proved one, both fail the suite.

    from acceptance import case

    class EvidenceTests(unittest.TestCase):
        @case("E01", "E02")
        def test_offsets_are_code_points(self):
            ...
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Callable, Iterable, TypeVar

REPO_ROOT = Path(__file__).parents[1]
CASE_FILE = REPO_ROOT / "evals" / "knowledge_v2" / "acceptance_cases.json"

ATTR = "__acceptance_cases__"
Method = TypeVar("Method", bound=Callable)


def case(*case_ids: str) -> Callable[[Method], Method]:
    """Mark a test method as the proof for these acceptance case ids."""

    def decorate(function: Method) -> Method:
        existing = tuple(getattr(function, ATTR, ()))
        setattr(function, ATTR, existing + tuple(case_ids))
        return function

    return decorate


def spec_cases() -> dict[str, dict]:
    document = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    return {entry["case_id"]: entry for entry in document["cases"]}


def _methods(suite: unittest.TestSuite) -> Iterable[tuple[str, Callable]]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _methods(item)
            continue
        test = item  # a TestCase instance
        name = test.id()
        function = getattr(type(test), getattr(test, "_testMethodName", ""), None)
        if callable(function):
            yield name, function


def _discovered_suite() -> unittest.TestSuite:
    """The same suite `python -m unittest discover -s tests` builds.

    Discovery is run the way CI runs it, from the repo root with `tests` as the
    start directory, so the test ids in the map are the ids a failing CI run
    prints. Scanning the directory instead would invent a second naming scheme.
    """

    loader = unittest.TestLoader()
    current = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        if str(REPO_ROOT / "tests") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "tests"))
        return loader.discover("tests")
    finally:
        os.chdir(current)


_DISCOVERED: dict[str, list[str]] = {}


def covered_cases() -> dict[str, list[str]]:
    """case_id -> sorted test ids that claim it, discovered by walking the suite."""

    if _DISCOVERED:
        return {case_id: list(tests) for case_id, tests in _DISCOVERED.items()}
    found: dict[str, list[str]] = {}
    for test_id, function in _methods(_discovered_suite()):
        for case_id in getattr(function, ATTR, ()):
            found.setdefault(case_id, []).append(test_id)
    _DISCOVERED.update({case_id: sorted(tests) for case_id, tests in found.items()})
    return {case_id: list(tests) for case_id, tests in found.items()}


def case_map() -> dict[str, object]:
    """The report the release checklist reads, computed rather than declared."""

    spec = spec_cases()
    covered = covered_cases()
    return {
        "spec_count": len(spec),
        "covered_count": len([case_id for case_id in spec if case_id in covered]),
        "uncovered": sorted(case_id for case_id in spec if case_id not in covered),
        "unknown": sorted(case_id for case_id in covered if case_id not in spec),
        "map": {case_id: covered.get(case_id, []) for case_id in sorted(spec)},
    }
