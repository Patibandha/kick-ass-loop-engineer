# tests/test_playbook.py
"""Tests for cross-run playbook governance."""
import os, tempfile, unittest
from kickass_loop_engineer.playbook import Playbook


class PlaybookTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "playbook.json")

    def test_promotes_only_after_three_successes(self):
        pb = Playbook(self.path, promote_after=3)
        for _ in range(2):
            pb.record_attempt("use-worktrees", success=True)
        self.assertFalse(pb.is_promoted("use-worktrees"))
        pb.record_attempt("use-worktrees", success=True)
        self.assertTrue(pb.is_promoted("use-worktrees"))

    def test_failure_does_not_count_toward_promotion(self):
        pb = Playbook(self.path, promote_after=3)
        pb.record_attempt("x", success=True)
        pb.record_attempt("x", success=False)
        pb.record_attempt("x", success=True)
        self.assertFalse(pb.is_promoted("x"))

    def test_stale_lesson_is_not_promoted_and_persists(self):
        pb = Playbook(self.path, promote_after=1)
        pb.record_attempt("y", success=True)
        self.assertTrue(pb.is_promoted("y"))
        pb.mark_stale("y")
        self.assertFalse(pb.is_promoted("y"))
        self.assertTrue(Playbook(self.path).is_stale("y"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
