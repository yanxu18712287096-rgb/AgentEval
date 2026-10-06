from feed.api import feed
from feed.repository import Repository

repo = Repository([{"tenant": "a", "id": 1, "created_at": 10}, {"tenant": "a", "id": 2, "created_at": 20}])
page = feed(repo, "a", 1)
assert [r["id"] for r in page["items"]] == [1]
assert [r["id"] for r in feed(repo, "a", 1, page["next_cursor"])["items"]] == [2]
print("public feed checks passed")
