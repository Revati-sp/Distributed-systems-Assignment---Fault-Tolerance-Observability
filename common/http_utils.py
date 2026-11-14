"""Tiny JSON helpers on top of the standard-library HTTP server."""

import json
from http.server import BaseHTTPRequestHandler

TRACE_HEADER = "X-Trace-ID"


class BadRequest(Exception):
    """Raised when a request body is not valid JSON or is missing fields."""


class JsonRequestHandler(BaseHTTPRequestHandler):
    """Base handler with JSON read/write helpers. Subclasses implement routes."""

    server_version = "Module9Lab/1.0"

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest("request body is not valid JSON: %s" % exc)
        if not isinstance(data, dict):
            raise BadRequest("request body must be a JSON object")
        return data

    def send_json(self, status_code, payload, trace_id=None):
        """Send a JSON response. Returns False if the caller already hung up."""
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if trace_id:
                self.send_header(TRACE_HEADER, trace_id)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            # The client (e.g. Checkout after its timeout) closed the connection.
            return False

    def log_message(self, format, *args):  # noqa: A002 - signature from base class
        # Silence the default unstructured access log; we write JSON logs instead.
        pass
