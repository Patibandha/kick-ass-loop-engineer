"""Tests for blocking severities and for persisting a slice's review findings.

Two gaps this closes, both seen on a real run (2026-09-21):

* review was purely ADVISORY, so a slice promoted with HIGH findings against
  it and the run still ended "complete";
* findings were never written anywhere. They went into the next slice's
  feedback and vanished, so afterwards nobody could say what a review had
  actually found — or whether it had found anything at all.
"""
import json
import tempfile
import unittest
from pathlib import Path

from kickass_loop_engineer.review import blocking_findings, write_slice_review

_FINDINGS = [
    "FINDING: [HIGH] pager.py:12 - offset skips rows - silent data loss",
    "FINDING: [LOW] cli.py:3 - message lists no ids",
]


class BlockingFindingsTests(unittest.TestCase):
    """Which findings, if any, must stop a slice from promoting."""

    def test_no_configured_severities_blocks_nothing(self):
        """Default stays advisory: existing runs do not change behaviour."""
        self.assertEqual(blocking_findings(_FINDINGS, ()), [])

    def test_a_high_finding_blocks_when_high_is_configured(self):
        self.assertEqual(blocking_findings(_FINDINGS, ("HIGH",)), [_FINDINGS[0]])

    def test_only_the_configured_severities_block(self):
        self.assertEqual(blocking_findings(_FINDINGS, ("CRITICAL",)), [])

    def test_severity_matching_ignores_case_and_brackets(self):
        found = blocking_findings(["finding: high - something bad"], ("HIGH",))
        self.assertEqual(len(found), 1)

    def test_a_severity_word_inside_another_word_does_not_match(self):
        """`highlight` is not a HIGH finding."""
        self.assertEqual(blocking_findings(["FINDING: highlighting a nit"], ("HIGH",)), [])

    def test_empty_findings_block_nothing(self):
        self.assertEqual(blocking_findings([], ("HIGH", "CRITICAL")), [])


class WriteSliceReviewTests(unittest.TestCase):
    """Every review is written down, including the ones that found nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_findings_are_written_under_the_run_directory(self):
        path = write_slice_review(str(self.workspace), "funding-pager", _FINDINGS,
                                  reviewer="gemini-3-pro")
        self.assertTrue(Path(path).is_file())
        body = self.read(Path(path))
        self.assertEqual(body["slice"], "funding-pager")
        self.assertEqual(body["reviewer"], "gemini-3-pro")
        self.assertEqual(body["findings"], _FINDINGS)

    def test_a_clean_review_is_recorded_too(self):
        """"No findings" and "never ran" must not look the same afterwards."""
        path = write_slice_review(str(self.workspace), "cache", [], reviewer="gemini-3-pro")
        self.assertEqual(self.read(Path(path))["findings"], [])

    def test_a_skipped_review_says_so(self):
        path = write_slice_review(str(self.workspace), "cache", [],
                                  reviewer="gemini-3-pro", skipped_reason="provider failure")
        body = self.read(Path(path))
        self.assertEqual(body["skipped_reason"], "provider failure")

    def test_writing_is_never_fatal(self):
        """A review that cannot be filed must not kill a run whose gates passed."""
        self.assertEqual(
            write_slice_review(str(self.workspace / "nope" / "\0bad"), "s", [], reviewer="r"),
            "")


class ReviewBlockOnConfigTests(unittest.TestCase):
    """`review.block_on` must reach the orchestrator, and fail loudly if mistyped."""

    def test_missing_section_is_advisory(self):
        from kickass_loop_engineer.config import review_block_on
        self.assertEqual(review_block_on({}), ())

    def test_configured_severities_are_returned_in_order(self):
        from kickass_loop_engineer.config import review_block_on
        self.assertEqual(review_block_on({"review": {"block_on": ["HIGH", "CRITICAL"]}}),
                         ("HIGH", "CRITICAL"))

    def test_blank_entries_are_dropped(self):
        from kickass_loop_engineer.config import review_block_on
        self.assertEqual(review_block_on({"review": {"block_on": ["HIGH", "  "]}}), ("HIGH",))

    def test_a_bare_string_is_a_config_error_not_a_silent_disarm(self):
        """`block_on: HIGH` must not quietly become nothing."""
        from kickass_loop_engineer.config import review_block_on
        with self.assertRaises(RuntimeError) as ctx:
            review_block_on({"review": {"block_on": "HIGH"}})
        self.assertIn("list of severities", str(ctx.exception))

    def test_a_non_mapping_review_section_is_a_config_error(self):
        from kickass_loop_engineer.config import review_block_on
        with self.assertRaises(RuntimeError):
            review_block_on({"review": ["HIGH"]})


if __name__ == "__main__":
    unittest.main()
