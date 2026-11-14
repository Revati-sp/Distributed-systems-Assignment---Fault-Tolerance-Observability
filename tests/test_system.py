"""Automated tests for the Checkout/Payment fault-tolerance lab.

Run from the project root:

    python3 -m unittest discover -s tests -v

The tests start the REAL services as separate processes on free ports, with
log files in a temporary directory, and talk to them over HTTP. Synchronisation
uses health-check / log polling with deadlines (wait_until), not fixed sleeps.
Timing assertions use generous bounds so they are not flaky.
"""

import os
import shutil
import tempfile
import unittest
import uuid

from common.client import ServiceProcess, find_free_port, http_json, read_log, wait_until
from common.metrics import RequestMetrics

CHECKOUT_TIMEOUT = 1.0   # seconds
FAILURE_DELAY = 2.0      # seconds; deliberately > CHECKOUT_TIMEOUT

ENV = {}  # filled by setUpModule


def setUpModule():
    tmp = tempfile.mkdtemp(prefix="module9-tests-")
    payment_port, checkout_port = find_free_port(), find_free_port()
    ENV.update({
        "tmp": tmp,
        "payment_url": "http://127.0.0.1:%d" % payment_port,
        "checkout_url": "http://127.0.0.1:%d" % checkout_port,
        "payment_log": os.path.join(tmp, "payment.log"),
        "checkout_log": os.path.join(tmp, "checkout.log"),
    })
    ENV["payment"] = ServiceProcess("payment_service.app", {
        "PAYMENT_PORT": str(payment_port),
        "PAYMENT_DELAY_SECONDS": "0",
        "PAYMENT_LOG_FILE": ENV["payment_log"],
    }, ENV["payment_url"] + "/health").start()
    ENV["checkout"] = ServiceProcess("checkout_service.app", {
        "CHECKOUT_PORT": str(checkout_port),
        "PAYMENT_URL": ENV["payment_url"],
        "CHECKOUT_TIMEOUT_SECONDS": str(CHECKOUT_TIMEOUT),
        "CHECKOUT_LOG_FILE": ENV["checkout_log"],
    }, ENV["checkout_url"] + "/health").start()


def tearDownModule():
    for name in ("checkout", "payment"):
        if name in ENV:
            ENV[name].stop()
    shutil.rmtree(ENV.get("tmp", ""), ignore_errors=True)


# ---- helpers -------------------------------------------------------------------
def checkout(order_id="order-test", trace_id=None, amount=10.0):
    headers = {"X-Trace-ID": trace_id} if trace_id else {}
    return http_json("POST", ENV["checkout_url"] + "/checkout",
                     {"order_id": order_id, "amount": amount}, headers, timeout=30)


def metrics(service):
    return http_json("GET", ENV[service + "_url"] + "/metrics")[2]


def set_delay(seconds):
    return http_json("POST", ENV["payment_url"] + "/admin/delay", {"delay_seconds": seconds})


def events(service, trace_id):
    return [e["event"] for e in read_log(ENV[service + "_log"], trace_id)]


# ---- 1. normal operation ---------------------------------------------------------
class Test1NormalOperation(unittest.TestCase):
    def setUp(self):
        set_delay(0)

    def test_01_checkout_health(self):
        status, _, body = http_json("GET", ENV["checkout_url"] + "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "checkout")
        self.assertEqual(body["payment_timeout_seconds"], CHECKOUT_TIMEOUT)

    def test_02_payment_health(self):
        status, _, body = http_json("GET", ENV["payment_url"] + "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "payment")

    def test_02b_services_are_separate_processes(self):
        c = http_json("GET", ENV["checkout_url"] + "/health")[2]
        p = http_json("GET", ENV["payment_url"] + "/health")[2]
        self.assertNotEqual(c["pid"], p["pid"])

    def test_03_payment_request_succeeds(self):
        trace = "direct-" + uuid.uuid4().hex
        status, headers, body = http_json("POST", ENV["payment_url"] + "/pay",
                                          {"order_id": "o-1", "amount": 5}, {"X-Trace-ID": trace})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["trace_id"], trace)
        self.assertTrue(body["payment_id"].startswith("pay-"))

    def test_04_checkout_request_succeeds(self):
        status, _, body = checkout("order-ok")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["payment"]["status"], "success")
        self.assertEqual(body["order_id"], "order-ok")
        self.assertLess(body["latency_ms"], CHECKOUT_TIMEOUT * 1000)

    def test_05_trace_id_generated_when_absent(self):
        status, headers, body = checkout()
        self.assertEqual(status, 200)
        uuid.UUID(body["trace_id"])  # raises ValueError if not a valid UUID
        self.assertEqual(headers.get("X-Trace-ID"), body["trace_id"])
        started = [e for e in read_log(ENV["checkout_log"], body["trace_id"])
                   if e["event"] == "request_started"]
        self.assertEqual(started[0]["trace_id_source"], "generated")

    def test_06_existing_trace_id_preserved(self):
        trace = "client-" + uuid.uuid4().hex
        status, headers, body = checkout(trace_id=trace)
        self.assertEqual(status, 200)
        self.assertEqual(body["trace_id"], trace)
        self.assertEqual(headers.get("X-Trace-ID"), trace)

    def test_07_checkout_forwards_same_trace_id_to_payment(self):
        trace = "forward-" + uuid.uuid4().hex
        _, _, body = checkout(trace_id=trace)
        # Payment echoes the header it actually received over HTTP ...
        self.assertEqual(body["payment"]["trace_id"], trace)
        # ... and logged it in its own (separate) log file.
        self.assertIn("payment_started", events("payment", trace))
        self.assertIn("payment_completed", events("payment", trace))

    def test_08_request_logs_contain_trace_id(self):
        _, _, body = checkout()
        trace = body["trace_id"]
        entries = read_log(ENV["checkout_log"], trace)
        self.assertEqual([e["event"] for e in entries],
                         ["request_started", "payment_call_started",
                          "payment_response_received", "request_completed"])
        done = entries[-1]
        for field in ("timestamp", "service", "trace_id", "event", "status", "latency_ms"):
            self.assertIn(field, done)
        self.assertEqual(done["status"], "success")
        self.assertGreater(done["latency_ms"], 0)

    def test_09_metrics_count_total_requests(self):
        c0, p0 = metrics("checkout"), metrics("payment")
        checkout()
        checkout()
        c1, p1 = metrics("checkout"), metrics("payment")
        self.assertEqual(c1["total_requests"], c0["total_requests"] + 2)
        self.assertEqual(p1["total_requests"], p0["total_requests"] + 2)

    def test_10_success_counter_increments_and_latency_recorded(self):
        c0, p0 = metrics("checkout"), metrics("payment")
        checkout()
        c1, p1 = metrics("checkout"), metrics("payment")
        self.assertEqual(c1["success_count"], c0["success_count"] + 1)
        self.assertEqual(p1["success_count"], p0["success_count"] + 1)
        self.assertEqual(c1["failure_count"], c0["failure_count"])
        self.assertIsNotNone(c1["last_latency_ms"])
        self.assertIsNotNone(p1["average_latency_ms"])


# ---- 2. failure + recovery scenario (runs once, many assertions) ----------------
class Test2FailureAndRecovery(unittest.TestCase):
    """One realistic scenario: delay on -> timeout -> delay off -> recovery."""

    RECOVERY_REQUESTS = 4

    @classmethod
    def setUpClass(cls):
        set_delay(0)
        _, _, cls.normal = checkout("order-normal")  # baseline latency
        cls.checkout_pid = http_json("GET", ENV["checkout_url"] + "/health")[2]["pid"]
        cls.before = metrics("checkout")

        cls.delay_on = set_delay(FAILURE_DELAY)
        cls.delay_after_enable = http_json("GET", ENV["payment_url"] + "/admin/delay")[2]
        cls.failure_trace = "failure-" + uuid.uuid4().hex
        cls.fail_status, cls.fail_headers, cls.fail_body = checkout("order-slow", cls.failure_trace)
        cls.health_after_timeout = http_json("GET", ENV["checkout_url"] + "/health")
        cls.after_failure = metrics("checkout")

        # Deterministic sync: wait until Payment logs that it finished the slow request.
        cls.late_completion = wait_until(
            lambda: "payment_completed" in events("payment", cls.failure_trace),
            timeout=FAILURE_DELAY + 10)

        cls.delay_off = set_delay(0)
        cls.recovery = [checkout("order-recovered-%d" % i) for i in range(cls.RECOVERY_REQUESTS)]
        cls.after_recovery = metrics("checkout")
        cls.health_after_recovery = http_json("GET", ENV["checkout_url"] + "/health")

    def test_11_payment_delay_can_be_enabled(self):
        self.assertEqual(self.delay_on[0], 200)
        self.assertEqual(self.delay_after_enable["delay_seconds"], FAILURE_DELAY)

    def test_12_payment_delay_exceeds_checkout_timeout(self):
        timeout = self.health_after_timeout[2]["payment_timeout_seconds"]
        self.assertGreater(self.delay_after_enable["delay_seconds"], timeout)

    def test_13_checkout_detects_real_payment_timeout(self):
        self.assertEqual(self.fail_body["error"], "payment_timeout")
        # Real timeout: Checkout waited at least the timeout, but not the full Payment delay.
        self.assertGreaterEqual(self.fail_body["latency_ms"], CHECKOUT_TIMEOUT * 1000 * 0.95)
        self.assertLess(self.fail_body["latency_ms"], FAILURE_DELAY * 1000)
        # Payment really received the request (it was slow, not unreachable).
        self.assertIn("payment_started", events("payment", self.failure_trace))
        self.assertIn("payment_timeout", events("checkout", self.failure_trace))

    def test_14_timeout_returns_504(self):
        self.assertEqual(self.fail_status, 504)
        self.assertEqual(self.fail_body["status"], "failure")
        self.assertEqual(self.fail_body["trace_id"], self.failure_trace)

    def test_15_failure_counter_increments(self):
        self.assertEqual(self.after_failure["failure_count"], self.before["failure_count"] + 1)
        self.assertEqual(self.after_failure["total_requests"], self.before["total_requests"] + 1)

    def test_16_timeout_counter_increments(self):
        self.assertEqual(self.after_failure["timeout_count"], self.before["timeout_count"] + 1)
        self.assertEqual(self.after_failure["failures_by_reason"].get("payment_timeout", 0),
                         self.before["failures_by_reason"].get("payment_timeout", 0) + 1)

    def test_17_checkout_alive_after_timeout(self):
        status, _, body = self.health_after_timeout
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["pid"], self.checkout_pid)  # same process: not restarted
        self.assertTrue(ENV["checkout"].is_running())

    def test_18_failure_trace_id_in_both_services(self):
        c = read_log(ENV["checkout_log"], self.failure_trace)
        p = read_log(ENV["payment_log"], self.failure_trace)
        self.assertTrue(c and p)
        self.assertTrue(all(e["trace_id"] == self.failure_trace for e in c + p))
        self.assertEqual({e["service"] for e in c}, {"checkout"})
        self.assertEqual({e["service"] for e in p}, {"payment"})
        timeout_entry = [e for e in c if e["event"] == "payment_timeout"][0]
        self.assertEqual(timeout_entry["status"], "failure")

    def test_18b_payment_finishes_after_checkout_gave_up(self):
        # Edge case: a Checkout timeout does not mean Payment failed.
        self.assertTrue(self.late_completion)
        timeout_ts = [e["timestamp"] for e in read_log(ENV["checkout_log"], self.failure_trace)
                      if e["event"] == "payment_timeout"][0]
        done = [e for e in read_log(ENV["payment_log"], self.failure_trace)
                if e["event"] == "payment_completed"][0]
        self.assertEqual(done["status"], "success")
        self.assertGreater(done["timestamp"], timeout_ts)

    def test_19_payment_delay_can_be_removed(self):
        self.assertEqual(self.delay_off[0], 200)
        self.assertEqual(self.delay_off[2]["delay_seconds"], 0.0)
        self.assertEqual(self.delay_off[2]["previous_delay_seconds"], FAILURE_DELAY)

    def test_20_requests_succeed_after_recovery(self):
        for status, _, body in self.recovery:
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "success")
            self.assertEqual(body["payment"]["trace_id"], body["trace_id"])
        status, _, body = self.health_after_recovery
        self.assertEqual(body["pid"], self.checkout_pid)  # Checkout never restarted

    def test_21_error_rate_decreases_after_recovery(self):
        self.assertGreater(self.after_failure["error_rate"], self.before["error_rate"])
        self.assertLess(self.after_recovery["error_rate"], self.after_failure["error_rate"])
        self.assertEqual(self.after_recovery["failure_count"], self.after_failure["failure_count"])
        self.assertEqual(self.after_recovery["success_count"],
                         self.after_failure["success_count"] + self.RECOVERY_REQUESTS)

    def test_22_latency_returns_to_normal_range(self):
        failure_ms = self.fail_body["latency_ms"]
        for _, _, body in self.recovery:
            self.assertLess(body["latency_ms"], CHECKOUT_TIMEOUT * 1000 * 0.5)
            self.assertLess(body["latency_ms"], failure_ms * 0.5)


# ---- 3. extra edge cases -------------------------------------------------------
class Test3EdgeCases(unittest.TestCase):
    def test_payment_does_not_invent_trace_id(self):
        status, _, body = http_json("POST", ENV["payment_url"] + "/pay", {"order_id": "o", "amount": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "missing_trace_id")

    def test_invalid_checkout_request_returns_400_with_trace(self):
        trace = "bad-" + uuid.uuid4().hex
        status, _, body = http_json("POST", ENV["checkout_url"] + "/checkout",
                                    {"order_id": "", "amount": -1}, {"X-Trace-ID": trace})
        self.assertEqual(status, 400)
        self.assertEqual(body["trace_id"], trace)

    def test_invalid_delay_rejected(self):
        status, _, _ = set_delay(-5)
        self.assertEqual(status, 400)

    def test_checkout_survives_payment_being_down(self):
        """Different failure mode: Payment unreachable -> 502, Checkout keeps running."""
        dead_port, port = find_free_port(), find_free_port()
        service = ServiceProcess("checkout_service.app", {
            "CHECKOUT_PORT": str(port),
            "PAYMENT_URL": "http://127.0.0.1:%d" % dead_port,
            "CHECKOUT_LOG_FILE": os.path.join(ENV["tmp"], "checkout-down.log"),
        }, "http://127.0.0.1:%d/health" % port).start()
        try:
            status, _, body = http_json("POST", "http://127.0.0.1:%d/checkout" % port,
                                        {"order_id": "o", "amount": 1})
            self.assertEqual(status, 502)
            self.assertEqual(body["error"], "payment_unavailable")
            self.assertTrue(service.is_healthy())
        finally:
            service.stop()


class Test4MetricsUnit(unittest.TestCase):
    def test_error_rate_math(self):
        m = RequestMetrics("x", track_timeouts=True)
        self.assertEqual(m.snapshot()["error_rate"], 0.0)
        m.record(True, 10)
        m.record(False, 1000, failure_reason="payment_timeout", timeout=True)
        s = m.snapshot()
        self.assertEqual((s["total_requests"], s["error_rate"], s["timeout_count"]), (2, 0.5, 1))
        for _ in range(3):
            m.record(True, 10)
        s = m.snapshot()
        self.assertEqual(s["error_rate"], 0.2)
        self.assertEqual(s["average_latency_ms"], 208.0)
        self.assertEqual(s["max_latency_ms"], 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
