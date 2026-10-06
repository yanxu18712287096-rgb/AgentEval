import unittest
from documents.repository import Repository
from documents.cache import Cache
from documents.service import DocumentService


class Requirements(unittest.TestCase):
    def setUp(self):
        self.original = dict(id="d", owner="alice", shared=["bob"], title="One", body="Text", revision=1)
        self.repo = Repository([self.original, dict(id="private", owner="carol", shared=[], title="Secret", body="Hidden", revision=1)])
        self.service = DocumentService(self.repo, Cache())

    def test_R1_authorize_every_read(self):
        self.service.read("alice", "d")
        with self.assertRaises(PermissionError):
            self.service.read("mallory", "d")
        self.service.read("bob", "d")
        self.repo.update("d", shared=[])
        with self.assertRaises(PermissionError):
            self.service.read("bob", "d")
        with self.assertRaises(KeyError):
            self.service.read("alice", "missing")

    def test_R2_revision_and_freshness(self):
        self.service.read("alice", "d")
        self.repo.update("d", title="Two")
        value = self.service.read("alice", "d")
        self.assertEqual(value, dict(id="d", title="Two", body="Text", revision=2))
        self.repo.update("d", title="Two", body="Changed")
        self.assertEqual(self.service.read("bob", "d")["revision"], 3)
        self.assertEqual(self.service.read("bob", "d")["body"], "Changed")
        self.repo.update("d", body="Changed")
        self.assertEqual(self.repo.get("d")["revision"], 4)

    def test_R3_snapshot_isolation(self):
        self.original["shared"].append("mallory")
        value = self.service.read("alice", "d")
        value["title"] = "poison"
        self.assertEqual(self.service.read("bob", "d")["title"], "One")
        shared = ["bob"]
        self.repo.update("d", shared=shared)
        shared.append("mallory")
        self.assertEqual(self.repo.get("d")["shared"], ["bob"])

    def test_R4_batch_authorization(self):
        self.service.read("carol", "private")
        with self.assertRaises(PermissionError):
            self.service.read_many("alice", ["d", "private"])
        self.repo.update("private", shared=["alice"])
        self.assertEqual([r["id"] for r in self.service.read_many("alice", ["private", "d"])], ["private", "d"])


if __name__ == "__main__":
    unittest.main()
