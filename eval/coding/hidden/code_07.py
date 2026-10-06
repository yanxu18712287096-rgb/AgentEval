from target import sort_jobs

jobs = [{"id": "first", "priority": 2}, {"id": "lower", "priority": 1},
        {"id": "second", "priority": 2}]
assert [j["id"] for j in sort_jobs(jobs)] == ["first", "second", "lower"]
