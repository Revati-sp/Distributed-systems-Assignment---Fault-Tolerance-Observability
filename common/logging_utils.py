"""Structured JSON-lines logging shared by both services.

Every log entry is one JSON object per line with the same core fields:

    timestamp, service, trace_id, event, status, latency_ms

plus any extra fields passed by the caller. One-object-per-line makes the
logs easy to grep (``grep <trace_id> evidence/*.log``) and easy to parse.
"""

import json
import os
import threading
from datetime import datetime, timezone


def utc_timestamp():
    """ISO-8601 UTC timestamp with microsecond precision, e.g. 2026-11-13T20:45:01.123456Z"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class JsonLogger:
    """Writes structured log entries to stdout and (optionally) to a log file."""

    def __init__(self, service, log_file=None):
        self.service = service
        self.log_file = log_file
        self._lock = threading.Lock()  # requests are handled on multiple threads
        if log_file:
            directory = os.path.dirname(log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)

    def log(self, event, trace_id=None, status=None, latency_ms=None, **fields):
        entry = {
            "timestamp": utc_timestamp(),
            "service": self.service,
            "trace_id": trace_id,
            "event": event,
            "status": status,
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
        }
        entry.update(fields)
        line = json.dumps(entry)
        with self._lock:
            print(line, flush=True)
            if self.log_file:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        return entry
