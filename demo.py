#!/usr/bin/env python3
"""End-to-end demo: success -> Payment slowdown/timeout -> recovery.

    python3 demo.py

Starts the Payment and Checkout services as two separate processes, then:

  PHASE 1  normal request succeeds (Checkout -> Payment over HTTP)
  PHASE 2  Payment delay (2.0s) > Checkout timeout (1.0s): real HTTP timeout,
           Checkout returns 504 and stays alive; the failed request is followed
           across both services' logs by its trace ID
  PHASE 3  delay removed (Checkout untouched): requests succeed again, the
           cumulative error rate decreases and latency returns to normal

Every value printed comes from the running services (HTTP responses, /metrics,
/health and the JSON log files). Nothing is hardcoded. Output is also saved to
evidence/demo-output.txt; service logs go to evidence/checkout.log and
evidence/payment.log.
"""

import argparse
import json
import os
import sys
import uuid

from common.client import PROJECT_ROOT, ServiceProcess, http_json, port_is_free, read_log, wait_until

EVIDENCE_DIR = os.path.join(PROJECT_ROOT, "evidence")
LINE = "=" * 50


class Tee:
    """Print to the terminal and to a file at the same time."""

    def __init__(self, path):
        self.file = open(path, "w", encoding="utf-8")

    def __call__(self, text=""):
        print(text, flush=True)
        self.file.write(text + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def fmt_ms(value):
    return "%.2f ms" % value


def fmt_entry(entry):
    """One log entry as a compact, grep-friendly line."""
    parts = ["%-8s" % entry["service"].upper(), entry["timestamp"],
             "trace_id=%s" % entry["trace_id"], "event=%s" % entry["event"]]
    if entry.get("status") is not None:
        parts.append("status=%s" % entry["status"])
    if entry.get("latency_ms") is not None:
        parts.append("latency_ms=%s" % entry["latency_ms"])
    for key in ("http_status", "reason", "delay_seconds", "timeout_seconds"):
        if key in entry and entry[key] is not None and entry["event"] != "service_started":
            parts.append("%s=%s" % (key, entry[key]))
    return "  " + " ".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--checkout-port", type=int, default=8000)
    parser.add_argument("--payment-port", type=int, default=8001)
    parser.add_argument("--timeout", type=float, default=1.0, help="Checkout -> Payment timeout (s)")
    parser.add_argument("--failure-delay", type=float, default=2.0, help="Payment delay in PHASE 2 (s)")
    parser.add_argument("--recovery-requests", type=int, default=3)
    args = parser.parse_args()

    for port in (args.checkout_port, args.payment_port):
        if not port_is_free(port):
            sys.exit("Port %d is already in use. Stop whatever is using it, or pass "
                     "--checkout-port/--payment-port." % port)

    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    checkout_log = os.path.join(EVIDENCE_DIR, "checkout.log")
    payment_log = os.path.join(EVIDENCE_DIR, "payment.log")
    for path in (checkout_log, payment_log):  # fresh evidence for every run
        open(path, "w").close()

    checkout_url = "http://127.0.0.1:%d" % args.checkout_port
    payment_url = "http://127.0.0.1:%d" % args.payment_port

    payment = ServiceProcess("payment_service.app", {
        "PAYMENT_PORT": str(args.payment_port),
        "PAYMENT_DELAY_SECONDS": "0",
        "PAYMENT_LOG_FILE": os.path.join("evidence", "payment.log"),
    }, payment_url + "/health")
    checkout = ServiceProcess("checkout_service.app", {
        "CHECKOUT_PORT": str(args.checkout_port),
        "PAYMENT_URL": payment_url,
        "CHECKOUT_TIMEOUT_SECONDS": str(args.timeout),
        "CHECKOUT_LOG_FILE": os.path.join("evidence", "checkout.log"),
    }, checkout_url + "/health")

    out = Tee(os.path.join(EVIDENCE_DIR, "demo-output.txt"))
    try:
        payment.start()
        checkout.start()
        ok = run_demo(out, args, checkout_url, payment_url, checkout_log, payment_log)
    finally:
        checkout.stop()
        payment.stop()
        out.close()
    sys.exit(0 if ok else 1)


def run_demo(out, args, checkout_url, payment_url, checkout_log, payment_log):
    checks = []  # (description, passed)

    def check(description, passed):
        checks.append((description, bool(passed)))
        return "YES" if passed else "NO"

    def show_metrics(title):
        _, _, cm = http_json("GET", checkout_url + "/metrics")
        _, _, pm = http_json("GET", payment_url + "/metrics")
        out("%s" % title)
        out("  Checkout GET /metrics: " + json.dumps(cm))
        out("  Payment  GET /metrics: " + json.dumps(pm))
        return cm, pm

    def show_trace(trace_id):
        entries = read_log(checkout_log, trace_id) + read_log(payment_log, trace_id)
        entries.sort(key=lambda e: e["timestamp"])  # merge both services into one timeline
        for entry in entries:
            out(fmt_entry(entry))
        return entries

    def checkout_request(order_id, trace_id=None):
        headers = {"X-Trace-ID": trace_id} if trace_id else {}
        return http_json("POST", checkout_url + "/checkout",
                         {"order_id": order_id, "amount": 49.99}, headers, timeout=30)

    _, _, health0 = http_json("GET", checkout_url + "/health")
    checkout_pid = health0["pid"]
    timeout_s = health0["payment_timeout_seconds"]

    out(LINE)
    out("SETUP")
    out(LINE)
    out("Checkout Service: %s  (pid %d)" % (checkout_url, checkout_pid))
    out("Payment Service:  %s  (pid %d)" % (payment_url, http_json("GET", payment_url + "/health")[2]["pid"]))
    out("Two separate OS processes: %s" % check(
        "Checkout and Payment run as separate processes",
        checkout_pid != http_json("GET", payment_url + "/health")[2]["pid"]))
    out("Checkout -> Payment timeout: %.1f s" % timeout_s)
    out()

    # ------------------------------------------------------------------ PHASE 1
    out(LINE)
    out("PHASE 1: SUCCESSFUL REQUEST")
    out(LINE)
    _, _, d = http_json("POST", payment_url + "/admin/delay", {"delay_seconds": 0})
    out("Payment delay: NORMAL (%.1f s)" % d["delay_seconds"])
    out("Request: POST /checkout {\"order_id\": \"order-1001\", \"amount\": 49.99}  "
        "(no X-Trace-ID sent, so Checkout generates one)")
    out()
    status, headers, body = checkout_request("order-1001")
    trace1 = body.get("trace_id")
    payment_trace1 = body.get("payment", {}).get("trace_id")
    normal_latency = body.get("latency_ms")
    out("Trace ID: %s" % trace1)
    out()
    out("Checkout -> Payment")
    out()
    out("Checkout status: %s (HTTP %d)" % (body.get("status", "?").upper(), status))
    out("Payment status: %s (payment_id=%s)" % (body.get("payment", {}).get("status", "?").upper(),
                                              body.get("payment", {}).get("payment_id")))
    out("Checkout latency: %s" % fmt_ms(normal_latency))
    out("Trace ID echoed by Payment: %s" % payment_trace1)
    check("Phase 1 checkout succeeded", status == 200 and body.get("status") == "success")
    check("Phase 1 payment succeeded", body.get("payment", {}).get("status") == "success")
    p_entries = read_log(payment_log, trace1)
    out("Trace ID shared across services: %s (%s)" % (check(
        "Phase 1 trace ID reached Payment",
        payment_trace1 == trace1 and headers.get("X-Trace-ID") == trace1
        and any(e["event"] == "payment_completed" for e in p_entries)), trace1))
    out()
    out("Log entries for trace %s (both services, merged by timestamp):" % trace1)
    show_trace(trace1)
    out()
    m_before, _ = show_metrics("Metrics after PHASE 1:")
    out()

    # ------------------------------------------------------------------ PHASE 2
    out(LINE)
    out("PHASE 2: PAYMENT DELAY / TIMEOUT")
    out(LINE)
    _, _, d = http_json("POST", payment_url + "/admin/delay", {"delay_seconds": args.failure_delay})
    delay_s = http_json("GET", payment_url + "/admin/delay")[2]["delay_seconds"]
    out("Payment delay enabled: %.1f seconds   (POST %s/admin/delay)" % (delay_s, payment_url))
    out("Checkout timeout: %.1f seconds" % timeout_s)
    out("Payment delay > Checkout timeout: %s" % check("Payment delay exceeds Checkout timeout",
                                                      delay_s > timeout_s))
    out()
    trace2 = "failure-" + str(uuid.uuid4())  # this time the client supplies the trace ID
    out("Trace ID: %s   (supplied by the client in X-Trace-ID)" % trace2)
    out()
    out("Checkout -> Payment")
    status, headers, body = checkout_request("order-1002", trace2)
    failure_latency = body.get("latency_ms")
    started = [e for e in read_log(payment_log, trace2) if e["event"] == "payment_started"]
    out("Payment begins processing...   (payment.log: event=payment_started delay_seconds=%s)"
        % (started[0]["delay_seconds"] if started else "MISSING"))
    check("Payment received the delayed request", started)
    out("Checkout timeout detected.     (Checkout error=%s)" % body.get("error"))
    out()
    out("Checkout response: %s" % ("TIMEOUT" if body.get("error") == "payment_timeout"
                                    else body.get("status", "?").upper()))
    out("HTTP status: %d" % status)
    out("Checkout message: %s" % body.get("message"))
    out("Trace ID preserved in response: %s" % check(
        "Client-supplied trace ID preserved", body.get("trace_id") == trace2
        and headers.get("X-Trace-ID") == trace2))
    check("Checkout returned 504 payment_timeout", status == 504 and body.get("error") == "payment_timeout")
    real_wait = failure_latency is not None and timeout_s * 1000 <= failure_latency < delay_s * 1000
    out("Checkout latency: %s  (>= %.0f ms timeout and < %.0f ms Payment delay: Checkout really "
        "waited, then gave up) %s" % (fmt_ms(failure_latency), timeout_s * 1000, delay_s * 1000,
                                      check("Timeout latency is between timeout and delay", real_wait)))
    out()
    hstatus, _, health = http_json("GET", checkout_url + "/health")
    alive = hstatus == 200 and health["status"] == "ok" and health["pid"] == checkout_pid
    out("GET /health -> HTTP %d %s" % (hstatus, json.dumps(health)))
    out("Checkout service still running: %s  (same pid %d, never restarted)"
        % (check("Checkout healthy after timeout", alive), health["pid"]))
    out()
    m_fail, p_fail = show_metrics("Metrics immediately after the failure:")
    out("  Checkout failure_count: %d -> %d   timeout_count: %d -> %d   error_rate: %.1f%% -> %.1f%%"
        % (m_before["failure_count"], m_fail["failure_count"], m_before["timeout_count"],
           m_fail["timeout_count"], m_before["error_rate_percent"], m_fail["error_rate_percent"]))
    check("Failure counter increased", m_fail["failure_count"] == m_before["failure_count"] + 1)
    check("Timeout counter increased", m_fail["timeout_count"] == m_before["timeout_count"] + 1)
    out()

    # Distributed-systems subtlety: a Checkout timeout does NOT mean Payment failed.
    out("Waiting for Payment to finish the slow request (Payment is slow, not crashed)...")

    def late_completion():
        return [e for e in read_log(payment_log, trace2) if e["event"] == "payment_completed"]
    late = wait_until(late_completion, timeout=delay_s + 10)
    out("Payment finished the request AFTER Checkout gave up: %s (payment latency %s)"
        % (check("Payment completed the delayed request after the timeout", late),
           fmt_ms(late[0]["latency_ms"]) if late else "n/a"))
    out("=> Checkout stopped waiting because its timeout expired; Payment was intentionally slow")
    out("   and still completed. The client got a 504, so the outcome must be reconciled later")
    out("   (e.g. by order_id); the timeout is NOT proof that Payment crashed.")
    out()
    out("CROSS-SERVICE TRACE for the failed request (grep %s evidence/*.log):" % trace2)
    entries = show_trace(trace2)
    services = {e["service"] for e in entries}
    events = {(e["service"], e["event"]) for e in entries}
    out("Same trace ID found in Checkout AND Payment logs: %s" % check(
        "Failure trace ID appears in both services' logs",
        services == {"checkout", "payment"} and ("checkout", "payment_timeout") in events
        and ("payment", "payment_started") in events))
    out()

    # ------------------------------------------------------------------ PHASE 3
    out(LINE)
    out("PHASE 3: RECOVERY")
    out(LINE)
    _, _, d = http_json("POST", payment_url + "/admin/delay", {"delay_seconds": 0})
    out("Payment delay removed.   (POST /admin/delay {\"delay_seconds\": 0} -> %.1f s)"
        % d["delay_seconds"])
    out("Checkout was not changed, reconfigured or restarted.")
    out()
    recovery_latencies = []
    for i in range(1, args.recovery_requests + 1):
        status, headers, body = checkout_request("order-%d" % (2000 + i))
        trace = body.get("trace_id")
        ok = status == 200 and body.get("status") == "success"
        recovery_latencies.append(body.get("latency_ms"))
        check("Recovery request %d succeeded" % i, ok)
        check("Recovery request %d trace reached Payment" % i, body.get("payment", {}).get("trace_id") == trace)
        out("Recovery request %d/%d" % (i, args.recovery_requests))
        out("  Trace ID: %s" % trace)
        out("  Checkout -> Payment")
        out("  Checkout status: %s (HTTP %d)" % (body.get("status", "?").upper(), status))
        out("  Payment status: %s" % body.get("payment", {}).get("status", "?").upper())
        out("  Checkout latency: %s" % fmt_ms(body.get("latency_ms")))
        if i == 1:
            out("  Log entries for this trace:")
            show_trace(trace)
    out()
    hstatus, _, health = http_json("GET", checkout_url + "/health")
    out("Checkout service still running: %s  (same pid %d, uptime %.1f s)" % (check(
        "Checkout still running with same PID after recovery",
        hstatus == 200 and health["pid"] == checkout_pid), health["pid"], health["uptime_seconds"]))
    starts = [e for e in read_log(checkout_log) if e["event"] == "service_started"]
    out("Checkout service_started events in checkout.log: %d %s" % (
        len(starts), "(started once, never restarted)" if len(starts) == 1 else ""))
    check("Checkout started exactly once", len(starts) == 1)
    out()
    m_rec, p_rec = show_metrics("Metrics after recovery:")
    out()

    # ---- numeric comparisons ---------------------------------------------------
    out(LINE)
    out("ERROR RATE (from Checkout /metrics counters)")
    out(LINE)
    out("Before failure:  %5.1f%%  (%d failed / %d total)"
        % (m_before["error_rate_percent"], m_before["failure_count"], m_before["total_requests"]))
    out("After failure:   %5.1f%%  (%d failed / %d total)"
        % (m_fail["error_rate_percent"], m_fail["failure_count"], m_fail["total_requests"]))
    out("After recovery:  %5.1f%%  (%d failed / %d total)"
        % (m_rec["error_rate_percent"], m_rec["failure_count"], m_rec["total_requests"]))
    out()
    out("ERROR RATE DECREASED: %s" % check("Error rate decreased after recovery",
                                           m_rec["error_rate"] < m_fail["error_rate"]))
    out()

    out(LINE)
    out("LATENCY (measured inside Checkout, per request)")
    out(LINE)
    recovery_avg = sum(recovery_latencies) / len(recovery_latencies)
    out("Normal latency:   %10s" % fmt_ms(normal_latency))
    out("Failure latency:  %10s   (~ the %.0f ms timeout)" % (fmt_ms(failure_latency), timeout_s * 1000))
    out("Recovery latency: %10s   (average of %d requests: %s)"
        % (fmt_ms(recovery_avg), len(recovery_latencies),
           ", ".join("%.2f" % v for v in recovery_latencies)))
    out("Rule: recovery average < 50%% of the timeout (%.0f ms) and < 50%% of the failure latency"
        % (timeout_s * 500))
    out()
    out("LATENCY RETURNED TO NORMAL RANGE: %s" % check(
        "Latency returned to normal range",
        recovery_avg < timeout_s * 500 and recovery_avg < failure_latency * 0.5))
    out()

    # ---- summary ---------------------------------------------------------------
    out(LINE)
    out("SUMMARY")
    out(LINE)
    for description, passed in checks:
        out("[%s] %s" % ("PASS" if passed else "FAIL", description))
    all_ok = all(p for _, p in checks)
    out()
    out("RESULT: %s (%d/%d checks passed)" % ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED",
                                              sum(p for _, p in checks), len(checks)))
    return all_ok


if __name__ == "__main__":
    main()
