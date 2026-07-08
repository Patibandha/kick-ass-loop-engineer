# tests/test_notify.py
"""Tests for the notify hook."""
import json, os, tempfile, unittest
from kickass_loop_engineer.notify import Notifier, build_event


class NotifyTests(unittest.TestCase):
    def test_build_event_matches_contract(self):
        ev = build_event(run_id="r1", stage="verify", event="escalation_required",
                         level="escalate", payload={"why": "risky"})
        self.assertEqual(set(ev), {"ts", "run_id", "stage", "event", "level", "payload"})
        self.assertEqual(ev["event"], "escalation_required")
        self.assertEqual(ev["level"], "escalate")

    def test_command_hook_receives_event_json(self):
        out = os.path.join(tempfile.mkdtemp(), "got.json")
        n = Notifier(command=f"cat > {out}")
        ok = n.notify(build_event("r1", "verify", "terminal", "info", {"state": "success"}))
        self.assertTrue(ok)
        with open(out) as f:
            self.assertEqual(json.load(f)["event"], "terminal")

    def test_disabled_notifier_is_noop_and_safe(self):
        n = Notifier()
        self.assertFalse(n.notify(build_event("r1", "x", "terminal", "info", {})))

    def test_bad_command_never_raises(self):
        n = Notifier(command="/no/such/binary --x")
        self.assertFalse(n.notify(build_event("r1", "x", "terminal", "info", {})))


if __name__ == "__main__":
    unittest.main(verbosity=2)
