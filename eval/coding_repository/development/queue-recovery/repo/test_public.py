import tempfile
from pathlib import Path
from queueapp.store import Store
from queueapp.worker import run_one

with tempfile.TemporaryDirectory() as temp:
    path = Path(temp) / "jobs.json"
    store = Store(path)
    store.enqueue("a", {"value": 3})
    received = []
    assert run_one(store, "a", received.append, now=10)
    assert received == [{"value": 3}]
    assert Store(path).get("a")["status"] == "done"
    assert not run_one(store, "a", received.append, now=20)
print("public queue checks passed")
