import tempfile
import unittest
from pathlib import Path
from queueapp.store import Store
from queueapp.recovery import recover
from queueapp.worker import run_one


class Requirements(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.json"
        self.store = Store(self.path)

    def test_R1_attempt_accounting(self):
        s = self.store
        s.enqueue("x", {}, max_attempts=2)
        self.assertEqual(s.get("x")["attempts"], 0)
        self.assertEqual(s.claim("x", 0, 5)["attempts"], 1)
        s.finish("x", False)
        self.assertEqual(s.get("x")["status"], "pending")
        self.assertEqual(s.claim("x", 10, 5)["attempts"], 2)
        s.finish("x", False)
        self.assertEqual(s.get("x")["status"], "failed")
        s.enqueue("ok", {})
        run_one(s, "ok", lambda _: None, now=0)
        self.assertEqual(s.get("ok")["attempts"], 1)

    def test_R2_recovery_boundaries(self):
        s = self.store
        for name, maximum in (("retry", 3), ("exhausted", 1), ("future", 3), ("done", 3)):
            s.enqueue(name, {}, maximum)
            s.claim(name, 0, 6 if name == "future" else 5)
        s.finish("done", True)
        self.assertEqual(recover(s, 5), 2)
        self.assertEqual(s.get("retry")["status"], "pending")
        self.assertEqual(s.get("exhausted")["status"], "failed")
        self.assertIsNone(s.get("exhausted")["lease_until"])
        self.assertEqual(s.get("future")["status"], "running")
        self.assertEqual(s.get("done")["status"], "done")

    def test_R3_durable_idempotent(self):
        s = self.store
        s.enqueue("x", {}, 2)
        s.claim("x", 0, 5)
        self.assertEqual(Store(self.path).get("x")["attempts"], 1)
        self.assertEqual(recover(s, 7), 1)
        restored = Store(self.path)
        self.assertEqual(restored.get("x")["status"], "pending")
        self.assertEqual(recover(restored, 8), 0)
        received = []
        self.assertTrue(run_one(restored, "x", received.append, 9))
        self.assertEqual(restored.get("x")["attempts"], 2)

    def test_R4_claim_exclusion(self):
        s = self.store
        s.enqueue("x", {"n": 1})
        item = s.claim("x", 0, 5)
        item["payload"]["n"] = 99
        self.assertEqual(s.get("x")["payload"]["n"], 1)
        self.assertIsNone(s.claim("x", 1, 5))


if __name__ == "__main__":
    unittest.main()
