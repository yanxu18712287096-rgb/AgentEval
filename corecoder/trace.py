"""Append-only execution evidence, independent of compressed conversation history."""

import copy
import json
import threading
import time
import uuid
from pathlib import Path


class TraceRecorder:
    def __init__(self, path: Path | None = None, **metadata):
        self.run_id = uuid.uuid4().hex
        self.path = Path(path) if path else None
        self.events = []
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=False)
        self.record("run_started", **metadata)

    def record(self, event: str, **data):
        with self._lock:
            item = copy.deepcopy({"seq": len(self.events), "run_id": self.run_id,
                                  "time": time.time(), "event": event, **data})
            if self.path:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            self.events.append(item)
