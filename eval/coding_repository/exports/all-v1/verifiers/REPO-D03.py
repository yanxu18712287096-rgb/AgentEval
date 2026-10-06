import unittest
from inventory.stock import Stock
from inventory.ledger import Ledger
from inventory.service import Reservations


class Requirements(unittest.TestCase):
    def setUp(self):
        self.stock = Stock({"a": 8, "b": 2})
        self.ledger = Ledger()
        self.service = Reservations(self.stock, self.ledger)

    def test_R1_atomic_failure(self):
        for items in ({"a": 2, "b": 9}, {"a": 2, "missing": 1}):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.service.reserve("bad", items)
            self.assertEqual(self.stock.snapshot(), {"a": 8, "b": 2})
            self.assertEqual(self.ledger.records, {})

    def test_R2_idempotency_conflict(self):
        first = self.service.reserve("x", {"a": 2})
        self.assertEqual(self.service.reserve("x", {"a": 2}), first)
        self.assertEqual(self.stock.snapshot()["a"], 6)
        with self.assertRaises(ValueError):
            self.service.reserve("x", {"a": 1})
        self.assertEqual(self.stock.snapshot()["a"], 6)
        self.assertEqual(self.ledger.get("x")["items"], {"a": 2})

    def test_R3_release_once(self):
        self.service.reserve("x", {"a": 2})
        self.service.release("x")
        self.service.release("x")
        self.assertEqual(self.stock.snapshot()["a"], 8)
        self.assertEqual(self.service.reserve("x", {"a": 2})["status"], "released")
        self.assertEqual(self.stock.snapshot()["a"], 8)
        with self.assertRaises(ValueError):
            self.service.reserve("x", {"a": 1})
        self.assertEqual(self.stock.snapshot()["a"], 8)
        with self.assertRaises(KeyError):
            self.service.release("unknown")

    def test_R4_snapshot_isolation(self):
        items = {"a": 2, "b": 1}
        result = self.service.reserve("x", items)
        items["a"] = 99
        result["items"]["b"] = 99
        self.assertEqual(self.ledger.get("x")["items"], {"a": 2, "b": 1})
        self.service.release("x")
        self.assertEqual(self.stock.snapshot(), {"a": 8, "b": 2})


if __name__ == "__main__":
    unittest.main()
