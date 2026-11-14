"""Payment Service (default http://127.0.0.1:8001).

Endpoints
    POST /pay            process a payment; requires the X-Trace-ID header
    GET  /health         liveness check
    GET  /metrics        in-memory request metrics (JSON)
    GET  /admin/delay    show the artificial delay        (LAB DEMO ONLY)
    POST /admin/delay    set the artificial delay, body {"delay_seconds": 2}

The artificial delay simulates a slow downstream dependency. It is set from
PAYMENT_DELAY_SECONDS at startup and can be changed at runtime through
/admin/delay, so the demo can switch failure mode on and off without
restarting anything. /admin/delay exists only for this lab; a real payment
service would never expose it.

Run:  python3 -m payment_service.app
"""

import os
import signal
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

from common.http_utils import TRACE_HEADER, BadRequest, JsonRequestHandler
from common.logging_utils import JsonLogger, utc_timestamp
from common.metrics import RequestMetrics

SERVICE = "payment"
MAX_DELAY_SECONDS = 60.0


class DelaySetting:
    """Thread-safe holder for the artificial delay."""

    def __init__(self, seconds):
        self._lock = threading.Lock()
        self._seconds = float(seconds)

    def get(self):
        with self._lock:
            return self._seconds

    def set(self, seconds):
        with self._lock:
            previous, self._seconds = self._seconds, float(seconds)
            return previous


class PaymentHandler(JsonRequestHandler):
    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        if self.path == "/health":
            self.handle_health()
        elif self.path == "/metrics":
            self.send_json(200, self.server.metrics.snapshot())
        elif self.path == "/admin/delay":
            self.send_json(200, {"delay_seconds": self.server.delay.get()})
        else:
            self.send_json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):
        if self.path == "/pay":
            self.handle_pay()
        elif self.path == "/admin/delay":
            self.handle_set_delay()
        else:
            self.send_json(404, {"error": "not_found", "path": self.path})

    # ---- handlers ----------------------------------------------------------
    def handle_health(self):
        self.send_json(200, {
            "status": "ok",
            "service": SERVICE,
            "pid": os.getpid(),
            "started_at": self.server.started_at,
            "uptime_seconds": round(time.monotonic() - self.server.started_monotonic, 3),
            "delay_seconds": self.server.delay.get(),
        })

    def handle_pay(self):
        log, metrics = self.server.logger, self.server.metrics
        start = time.perf_counter()

        # Payment never invents its own trace ID: it must use the one Checkout sent,
        # otherwise the request could not be followed across services.
        trace_id = self.headers.get(TRACE_HEADER)
        if not trace_id:
            latency = (time.perf_counter() - start) * 1000
            metrics.record(False, latency, failure_reason="missing_trace_id")
            log.log("payment_rejected", trace_id=None, status="failure", latency_ms=latency,
                    reason="missing %s header" % TRACE_HEADER)
            self.send_json(400, {"status": "failure", "error": "missing_trace_id"})
            return

        try:
            body = self.read_json()
            order_id = body.get("order_id")
            amount = body.get("amount")
            if not isinstance(order_id, str) or not order_id:
                raise BadRequest("order_id must be a non-empty string")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
                raise BadRequest("amount must be a positive number")
        except BadRequest as exc:
            latency = (time.perf_counter() - start) * 1000
            metrics.record(False, latency, failure_reason="invalid_request")
            log.log("payment_rejected", trace_id=trace_id, status="failure", latency_ms=latency,
                    reason=str(exc))
            self.send_json(400, {"status": "failure", "error": "invalid_request",
                                 "message": str(exc), "trace_id": trace_id}, trace_id)
            return

        delay = self.server.delay.get()  # snapshot: changing it later does not affect this request
        log.log("payment_started", trace_id=trace_id, order_id=order_id, amount=amount,
                delay_seconds=delay)

        if delay > 0:
            time.sleep(delay)  # the intentional, configurable slowdown

        payment_id = "pay-" + uuid.uuid4().hex[:12]
        latency = (time.perf_counter() - start) * 1000
        metrics.record(True, latency)
        log.log("payment_completed", trace_id=trace_id, status="success", latency_ms=latency,
                order_id=order_id, payment_id=payment_id, delay_seconds=delay)

        delivered = self.send_json(200, {
            "status": "success",
            "trace_id": trace_id,       # echoed back so Checkout/tests can verify propagation
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "latency_ms": round(latency, 2),
        }, trace_id)
        if not delivered:
            # Expected after a Checkout timeout: Payment finished, but nobody was waiting.
            log.log("response_not_delivered", trace_id=trace_id, status="failure",
                    reason="caller closed the connection before Payment responded")

    def handle_set_delay(self):
        try:
            body = self.read_json()
            value = body.get("delay_seconds")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BadRequest("delay_seconds must be a number")
            if not 0 <= value <= MAX_DELAY_SECONDS:
                raise BadRequest("delay_seconds must be between 0 and %s" % MAX_DELAY_SECONDS)
        except BadRequest as exc:
            self.send_json(400, {"error": "invalid_request", "message": str(exc)})
            return
        previous = self.server.delay.set(value)
        self.server.logger.log("delay_changed", status="success",
                               previous_delay_seconds=previous, delay_seconds=float(value))
        self.send_json(200, {"delay_seconds": float(value), "previous_delay_seconds": previous,
                             "note": "lab demonstration endpoint only"})


def create_server(host, port, delay_seconds, log_file):
    server = ThreadingHTTPServer((host, port), PaymentHandler)
    server.daemon_threads = True
    server.logger = JsonLogger(SERVICE, log_file)
    server.metrics = RequestMetrics(SERVICE)
    server.delay = DelaySetting(delay_seconds)
    server.started_at = utc_timestamp()
    server.started_monotonic = time.monotonic()
    return server


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    host = os.environ.get("PAYMENT_HOST", "127.0.0.1")
    port = int(os.environ.get("PAYMENT_PORT", "8001"))
    delay = float(os.environ.get("PAYMENT_DELAY_SECONDS", "0"))
    log_file = os.environ.get("PAYMENT_LOG_FILE", os.path.join("logs", "payment.log"))

    server = create_server(host, port, delay, log_file)
    server.logger.log("service_started", status="success", pid=os.getpid(),
                      address="http://%s:%d" % (host, port), delay_seconds=delay)
    # Ctrl+C (SIGINT) and `kill` (SIGTERM) both shut the server down cleanly.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.logger.log("service_stopped", status="success", pid=os.getpid())
        server.server_close()


if __name__ == "__main__":
    main()
