from datetime import timezone
import unittest
from booking.store import Store
from booking.service import BookingService
from booking.timeutil import parse


def at(hour):
    return f"2026-01-01T{hour:02d}:00:00+00:00"


class Requirements(unittest.TestCase):
    def setUp(self):
        self.service = BookingService(Store())

    def test_R1_utc_and_validation(self):
        self.assertEqual(parse("2026-01-01T09:00:00+08:00"), parse(at(1)))
        self.assertEqual(parse(at(1)).tzinfo, timezone.utc)
        for value in ("2026-01-01T10:00:00", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse(value)
        for start, end in ((at(2), at(2)), (at(3), at(2)), ("bad", at(3))):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.service.create("bad", "r", start, end)
            self.assertEqual(self.service.store.all(), [])

    def test_R2_overlap_boundaries(self):
        s = self.service
        s.create("a", "r", at(1), at(2))
        s.create("adjacent", "r", at(2), at(3))
        s.create("other", "other", at(1), at(2))
        with self.assertRaises(ValueError):
            s.create("clash", "r", "2026-01-01T09:30:00+08:00", "2026-01-01T10:30:00+08:00")
        self.assertNotIn("clash", s.store.rows)
        s.cancel("a")
        s.create("replacement", "r", at(1), at(2))

    def test_R3_atomic_reschedule(self):
        s = self.service
        s.create("a", "r", at(1), at(2))
        s.create("b", "r", at(4), at(5))
        s.reschedule("a", at(1), at(2))
        before = s.store.get("a")
        for start, end in ((at(4), at(6)), (at(3), at(2))):
            with self.subTest(start=start), self.assertRaises(ValueError):
                s.reschedule("a", start, end)
            self.assertEqual(s.store.get("a"), before)
        value = s.reschedule("a", at(2), at(3))
        self.assertEqual(value, dict(id="a", room="r", start=at(2), end=at(3), status="active"))
        with self.assertRaises(KeyError):
            s.reschedule("missing", at(2), at(3))

    def test_R4_cancel_and_listing(self):
        s = self.service
        s.create("b", "b", at(3), at(4))
        s.create("a", "a", "2026-01-01T10:00:00+08:00", "2026-01-01T11:00:00+08:00")
        s.create("c", "c", at(3), at(4))
        self.assertEqual([r["id"] for r in s.list_active()], ["a", "b", "c"])
        s.list_active()[0]["status"] = "cancelled"
        self.assertEqual(len(s.list_active()), 3)
        s.cancel("a")
        s.cancel("a")
        self.assertEqual([r["id"] for r in s.list_active()], ["b", "c"])
        with self.assertRaises(KeyError):
            s.cancel("unknown")
        with self.assertRaises(ValueError):
            s.create("b", "new", at(8), at(9))


if __name__ == "__main__":
    unittest.main()
