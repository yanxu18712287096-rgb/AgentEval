from documents.repository import Repository
from documents.cache import Cache
from documents.service import DocumentService

repo = Repository([dict(id="d", owner="alice", shared=["bob"], title="One", body="Text", revision=1)])
service = DocumentService(repo, Cache())
assert service.read("alice", "d")["title"] == "One"
assert service.read("bob", "d")["body"] == "Text"
assert service.read_many("alice", ["d"])[0]["id"] == "d"
print("public document checks passed")
