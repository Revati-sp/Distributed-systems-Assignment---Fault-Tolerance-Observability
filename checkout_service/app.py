"""Checkout Service (default http://127.0.0.1:8000).

Endpoints
    POST /checkout   body {"order_id": "order-123", "amount": 49.99}
    GET  /health     liveness check (also reports PID + timeout config)
    GET  /metrics    in-memory request metrics (JSON)

For every checkout the service:
  1. uses the incoming X-Trace-ID header, or generates a UUID if absent,
  2. calls Payment over real HTTP (POST {PAYMENT_URL}/pay) forwarding the SAME
     X-Trace-ID, with a timeout of CHECKOUT_TIMEOUT_SECONDS,
  3. on success returns 200; on a Payment timeout catches the exception and
     returns 504 -- the Checkout process keeps running either way,
  4. logs every step as JSON and records latency + success/failure metrics.

Run:  python3 -m checkout_service.app
"""

import json
import os
import signal
import socket
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

from common.http_utils import TRACE_HEADER, BadRequest, JsonRequestHandler
from common.logging_utils import JsonLogger, utc_timestamp
from common.metrics import RequestMetrics

SERVICE = "checkout"


# ---- outbound call to Payment ----------------------------------------------
class PaymentTimeout(Exception):
    """Payment did not answer within the configured timeout."""


class PaymentUnavailable(Exception):
    """Payment could not be reached (connection refused/reset, etc.)."""


class PaymentError(Exception):
    """Payment answered, but with an error status."""


def call_payment(payment_url, trace_id, order_id, amount, timeout_seconds):
    """POST to Payment's /pay over HTTP, forwarding the trace ID unchanged.

    `timeout_seconds` is passed to the socket, so the timeout is enforced by the
    operating system / urllib: if Payment sends nothing back within that time,
    the blocking read raises socket.timeout and we translate it to PaymentTimeout.
    """
    request = urllib.request.Request(
        payment_url.rstrip("/") + "/pay",
        data=json.dumps({"order_id": order_id, "amount": amount}).encode("utf-8"),
        headers={"Content-Type": "application/json", TRACE_HEADER: trace_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:          # Payment replied with 4xx/5xx
        raise PaymentError("Payment returned HTTP %d" % exc.code)
    except urllib.error.URLError as exc:           # failure while connecting
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise PaymentTimeout("timed out connecting to Payment")
        raise PaymentUnavailable(str(exc.reason))
    except (socket.timeout, TimeoutError):         # connected, but no response in time
        raise PaymentTimeout("no response from Payment within %.1fs" % timeout_seconds)
    except (OSError, ValueError) as exc:           # reset connection / malformed reply
        raise PaymentUnavailable(str(exc))


# ---- HTTP handler -------------------------------------------------------------
class CheckoutHandler(JsonRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {
                "status": "ok",
                "service": SERVICE,
                "pid": os.getpid(),
                "started_at": self.server.started_at,
                "uptime_seconds": round(time.monotonic() - self.server.started_monotonic, 3),
                "payment_url": self.server.payment_url,
                "payment_timeout_seconds": self.server.timeout_seconds,
            })
        elif self.path == "/metrics":
            self.send_json(200, self.server.metrics.snapshot())
        else:
            self.send_json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):
        if self.path == "/checkout":
            self.handle_checkout()
        else:
            self.send_json(404, {"error": "not_found", "path": self.path})

    def handle_checkout(self):
        log, metrics = self.server.logger, self.server.metrics
        start = time.perf_counter()

        incoming = self.headers.get(TRACE_HEADER)
        trace_id = incoming or str(uuid.uuid4())
        log.log("request_started", trace_id=trace_id,
                trace_id_source="client" if incoming else "generated")

        def elapsed_ms():
            return (time.perf_counter() - start) * 1000

        def fail(http_status, reason, message, timeout=False):
            latency = elapsed_ms()
            metrics.record(False, latency, failure_reason=reason, timeout=timeout)
            log.log("request_completed", trace_id=trace_id, status="failure", latency_ms=latency,
                    http_status=http_status, reason=reason)
            self.send_json(http_status, {"status": "failure", "error": reason, "message": message,
                                         "trace_id": trace_id, "latency_ms": round(latency, 2)},
                           trace_id)

        # 1. Validate input.
        try:
            body = self.read_json()
            order_id = body.get("order_id")
            amount = body.get("amount")
            if not isinstance(order_id, str) or not order_id:
                raise BadRequest("order_id must be a non-empty string")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
                raise BadRequest("amount must be a positive number")
        except BadRequest as exc:
            fail(400, "invalid_request", str(exc))
            return

        # 2. Call Payment over HTTP with a timeout, forwarding the same trace ID.
        timeout = self.server.timeout_seconds
        log.log("payment_call_started", trace_id=trace_id, order_id=order_id,
                payment_url=self.server.payment_url, timeout_seconds=timeout)
        try:
            payment = call_payment(self.server.payment_url, trace_id, order_id, amount, timeout)
        except PaymentTimeout as exc:
            # Checkout stops WAITING; it does not know whether Payment eventually finishes.
            log.log("payment_timeout", trace_id=trace_id, status="failure", latency_ms=elapsed_ms(),
                    timeout_seconds=timeout, detail=str(exc))
            fail(504, "payment_timeout",
                 "Checkout stopped waiting for Payment after %.1fs. Payment may still finish "
                 "the request later." % timeout, timeout=True)
            return
        except PaymentUnavailable as exc:
            log.log("payment_unavailable", trace_id=trace_id, status="failure",
                    latency_ms=elapsed_ms(), detail=str(exc))
            fail(502, "payment_unavailable", "Payment service could not be reached: %s" % exc)
            return
        except PaymentError as exc:
            log.log("payment_error", trace_id=trace_id, status="failure",
                    latency_ms=elapsed_ms(), detail=str(exc))
            fail(502, "payment_error", str(exc))
            return

        log.log("payment_response_received", trace_id=trace_id, status=payment.get("status"),
                latency_ms=elapsed_ms(), payment_id=payment.get("payment_id"),
                payment_trace_id=payment.get("trace_id"))

        # 3. Success.
        latency = elapsed_ms()
        metrics.record(True, latency)
        log.log("request_completed", trace_id=trace_id, status="success", latency_ms=latency,
                http_status=200, order_id=order_id)
        self.send_json(200, {
            "status": "success",
            "trace_id": trace_id,
            "order_id": order_id,
            "amount": amount,
            "payment": payment,
            "latency_ms": round(latency, 2),
        }, trace_id)


def create_server(host, port, payment_url, timeout_seconds, log_file):
    server = ThreadingHTTPServer((host, port), CheckoutHandler)
    server.daemon_threads = True
    server.logger = JsonLogger(SERVICE, log_file)
    server.metrics = RequestMetrics(SERVICE, track_timeouts=True)
    server.payment_url = payment_url
    server.timeout_seconds = timeout_seconds
    server.started_at = utc_timestamp()
    server.started_monotonic = time.monotonic()
    return server


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    host = os.environ.get("CHECKOUT_HOST", "127.0.0.1")
    port = int(os.environ.get("CHECKOUT_PORT", "8000"))
    payment_url = os.environ.get("PAYMENT_URL", "http://127.0.0.1:8001")
    timeout = float(os.environ.get("CHECKOUT_TIMEOUT_SECONDS", "1.0"))
    log_file = os.environ.get("CHECKOUT_LOG_FILE", os.path.join("logs", "checkout.log"))

    server = create_server(host, port, payment_url, timeout, log_file)
    server.logger.log("service_started", status="success", pid=os.getpid(),
                      address="http://%s:%d" % (host, port), payment_url=payment_url,
                      timeout_seconds=timeout)
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
