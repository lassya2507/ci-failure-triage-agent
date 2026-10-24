"""Step-level tracing. Every tool call and LLM call becomes one JSON line,
so a bad triage can be replayed step by step afterwards."""
import json
import time
from pathlib import Path


class Tracer:
    def __init__(self, path=None):
        self.events = []
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")
        self.t0 = time.perf_counter()

    def log(self, kind: str, **fields):
        event = {"t": round(time.perf_counter() - self.t0, 4), "kind": kind, **fields}
        self.events.append(event)
        if self.path:
            with self.path.open("a") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        return event

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e["kind"] == kind)
