import base64
import json
import unittest
from feed.api import feed
from feed.cursor import encode, decode
from feed.repository import Repository


def row(i, time=10, tenant="a"):
    return {"id": i, "created_at": time, "tenant": tenant}


class Requirements(unittest.TestCase):
    def test_R1_order_and_ties(self):
        repo = Repository([row(9, 1, "b"), row(3), row(1), row(2), row(4, 20)])
        token, ids = None, []
        for _ in range(10):
            result = feed(repo, "a", 2, token)
            ids.extend(r["id"] for r in result["items"])
            token = result["next_cursor"]
            if token is None:
                break
        self.assertEqual(ids, [1, 2, 3, 4])

    def test_R2_terminal_pages(self):
        repo = Repository([row(1), row(2, 20), row(3, 30, "b")])
        self.assertIsNone(feed(repo, "a", 2)["next_cursor"])
        self.assertEqual(feed(repo, "none", 2), {"items": [], "next_cursor": None})
        token = encode("a", row(2, 20))
        self.assertEqual(feed(repo, "a", 2, token), {"items": [], "next_cursor": None})

    def test_R3_cursor_validation(self):
        token = encode("a", row(4, 30))
        self.assertEqual(decode(token, "a"), (30, 4))
        with self.assertRaises(ValueError):
            decode(token, "b")
        for value in (None, [], {}, {"time": "10", "id": 1, "tenant": "a"},
                      {"time": 10, "id": True, "tenant": "a"}):
            token = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode(token, "a")
        with self.assertRaises(ValueError):
            decode("!broken!", "a")

    def test_R4_copy_isolation(self):
        repo = Repository([row(1)])
        result = feed(repo, "a", 2)
        result["items"][0]["id"] = 99
        self.assertEqual(feed(repo, "a", 2)["items"][0]["id"], 1)


if __name__ == "__main__":
    unittest.main()
