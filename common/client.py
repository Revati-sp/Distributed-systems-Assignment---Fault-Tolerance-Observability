"""Client-side helpers used by demo.py and the automated tests.

Not used by the services themselves. Provides:
  * http_json()      - make an HTTP request and decode the JSON reply
  * ServiceProcess   - start/stop a service as a separate OS process
  * wait_until()     - poll a condition with a deadline (deterministic sync)
  * read_log()       - parse a JSON-lines log file, optionally filtered by trace ID
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def http_json(method, url, body=None, headers=None, timeout=10.0):
    """Return (status_code, response_headers, decoded_json_body). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else {}


def wait_until(predicate, timeout=10.0, interval=0.02):
    """Poll `predicate()` until it returns a truthy value or the deadline passes."""
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def port_is_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) != 0


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_log(path, trace_id=None):
    """Parse a JSON-lines log file. If trace_id is given, keep only matching entries."""
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if trace_id is None or entry.get("trace_id") == trace_id:
                entries.append(entry)
    return entries


class ServiceProcess:
    """Runs `python3 -m <module>` as a genuinely separate OS process."""

    def __init__(self, module, env, health_url):
        self.module = module
        self.env = env
        self.health_url = health_url
        self.process = None

    def start(self, ready_timeout=10.0):
        env = dict(os.environ)
        env.update(self.env)
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, "-m", self.module],
            cwd=PROJECT_ROOT, env=env,
            stdout=subprocess.DEVNULL,  # JSON logs also go to the log file
            stderr=None,                # crashes stay visible on the console
        )
        if not wait_until(self.is_healthy, timeout=ready_timeout):
            self.stop()
            raise RuntimeError("%s did not become healthy at %s" % (self.module, self.health_url))
        return self

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def is_healthy(self):
        if not self.is_running():
            return False
        try:
            status, _, _ = http_json("GET", self.health_url, timeout=0.5)
            return status == 200
        except OSError:
            return False

    def stop(self):
        if not self.is_running():
            return
        self.process.send_signal(signal.SIGTERM)  # graceful: service logs service_stopped
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
