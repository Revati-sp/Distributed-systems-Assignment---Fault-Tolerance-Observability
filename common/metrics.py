"""Small thread-safe, in-memory request metrics shared by both services.

Exposed by each service at ``GET /metrics`` as flat JSON, for example:

    {"total_requests": 5, "success_count": 4, "failure_count": 1,
     "timeout_count": 1, "error_rate": 0.2, "error_rate_percent": 20.0,
     "average_latency_ms": 205.3, ...}
"""

import threading


class RequestMetrics:
    def __init__(self, service, track_timeouts=False):
        self.service = service
        self.track_timeouts = track_timeouts  # only Checkout has an outbound timeout
        self._lock = threading.Lock()
        self.total_requests = 0
        self.success_count = 0
        self.failure_count = 0
        self.timeout_count = 0
        self.failures_by_reason = {}
        self._latency_sum_ms = 0.0
        self._latency_min_ms = None
        self._latency_max_ms = None
        self._latency_last_ms = None

    def record(self, success, latency_ms, failure_reason=None, timeout=False):
        """Record one finished request (success or failure) and its latency."""
        with self._lock:
            self.total_requests += 1
            if success:
                self.success_count += 1
            else:
                self.failure_count += 1
                reason = failure_reason or "unknown"
                self.failures_by_reason[reason] = self.failures_by_reason.get(reason, 0) + 1
                if timeout:
                    self.timeout_count += 1
            self._latency_sum_ms += latency_ms
            self._latency_last_ms = latency_ms
            if self._latency_min_ms is None or latency_ms < self._latency_min_ms:
                self._latency_min_ms = latency_ms
            if self._latency_max_ms is None or latency_ms > self._latency_max_ms:
                self._latency_max_ms = latency_ms

    def snapshot(self):
        """Return a consistent copy of all counters as a plain dict."""
        with self._lock:
            total = self.total_requests
            error_rate = (self.failure_count / total) if total else 0.0

            def r(value):
                return round(value, 2) if value is not None else None

            data = {
                "service": self.service,
                "total_requests": total,
                "success_count": self.success_count,
                "failure_count": self.failure_count,
            }
            if self.track_timeouts:
                data["timeout_count"] = self.timeout_count
            data.update({
                "error_rate": round(error_rate, 4),
                "error_rate_percent": round(error_rate * 100, 2),
                "average_latency_ms": r(self._latency_sum_ms / total) if total else None,
                "min_latency_ms": r(self._latency_min_ms),
                "max_latency_ms": r(self._latency_max_ms),
                "last_latency_ms": r(self._latency_last_ms),
                "failures_by_reason": dict(self.failures_by_reason),
            })
            return data
