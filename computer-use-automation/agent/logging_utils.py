"""Structured, redacted, append-only JSONL logging for runs.

Every discovery run and every replay run writes one JSONL file: one JSON
object per line, each with a timestamp, a run_id, an event type, and a
payload. This is the structured log of what the agent did and why, and it's
also what a human operator reads when deciding whether/how to intervene.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from .guardrails import redact_dict


class RunLogger:
    def __init__(self, path: str, run_id: str):
        self.path = path
        self.run_id = run_id
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._fh = open(path, "a")

    def log(self, event: str, **payload) -> None:
        record = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "run_id": self.run_id,
            "event": event,
            **redact_dict(payload),
        }
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()