import copy
import json
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.jobs = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        self.path.write_text(json.dumps(self.jobs, sort_keys=True))

    def enqueue(self, job_id, payload, max_attempts=3):
        if job_id in self.jobs:
            raise ValueError("duplicate job")
        self.jobs[job_id] = dict(id=job_id, payload=copy.deepcopy(payload), status="pending",
                                 attempts=0, max_attempts=max_attempts, lease_until=None)
        self.save()

    def get(self, job_id):
        return copy.deepcopy(self.jobs[job_id])

    def claim(self, job_id, now, lease_seconds):
        job = self.jobs[job_id]
        if job["status"] != "pending":
            return None
        job["status"] = "running"
        job["lease_until"] = now + lease_seconds
        self.save()
        return copy.deepcopy(job)

    def finish(self, job_id, success):
        job = self.jobs[job_id]
        if job["status"] != "running":
            raise ValueError("job is not running")
        job["attempts"] += 1
        job["lease_until"] = None
        job["status"] = "done" if success else (
            "pending" if job["attempts"] < job["max_attempts"] else "failed")
        self.save()
