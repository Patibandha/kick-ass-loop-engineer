# tests/test_memory.py
"""Tests for the SQLite shared memory store."""
import os, tempfile, unittest
from kickass_loop_engineer.memory import Memory


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "mem.db")

    def test_record_and_get_latest(self):
        m = Memory(self.db)
        m.record("api.contract", "v1", run_id="r1", kind="contract")
        m.record("api.contract", "v2", run_id="r2", kind="contract")
        self.assertEqual(m.get("api.contract"), "v2")

    def test_query_by_kind(self):
        m = Memory(self.db)
        m.record("a", "1", run_id="r1", kind="fact")
        m.record("b", "2", run_id="r1", kind="contract")
        facts = m.query(kind="fact")
        self.assertEqual([f["key"] for f in facts], ["a"])

    def test_persists_across_instances(self):
        Memory(self.db).record("k", "v", run_id="r1", kind="fact")
        self.assertEqual(Memory(self.db).get("k"), "v")

    def test_forget_removes_key(self):
        m = Memory(self.db)
        m.record("k", "v", run_id="r1", kind="fact")
        m.forget("k")
        self.assertIsNone(m.get("k"))

    def test_consolidate_marks_run_canonical(self):
        m = Memory(self.db)
        m.record("k", "v", run_id="r1", kind="fact")
        m.consolidate("r1")
        self.assertEqual(m.query(status="canonical")[0]["key"], "k")

    def test_forget_and_consolidate_return_row_counts(self):
        m = Memory(self.db)
        m.record("k", "v", run_id="r1", kind="fact")
        m.record("k", "v2", run_id="r1", kind="fact")
        self.assertEqual(m.consolidate("r1"), 2)
        self.assertEqual(m.forget("k"), 2)
        self.assertEqual(m.forget("missing"), 0)

    def test_context_manager_closes(self):
        with Memory(self.db) as m:
            m.record("k", "v", run_id="r1", kind="fact")
        self.assertEqual(Memory(self.db).get("k"), "v")


if __name__ == "__main__":
    unittest.main(verbosity=2)
