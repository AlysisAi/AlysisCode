"""Synthetic guest page; retains only verification booleans, never typed text."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from bots_human_control import PAGE

result = {"submissions": 0, "unicode_name": False, "private_entered": False}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(min(4096, int(self.headers.get("Content-Length", 0)))))
        result.update(
            submissions=data["submissions"],
            unicode_name=data["name"] == "Καλημέρα 雲",
            private_entered=bool(data["private_entered"]),
        )
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        payload = json.dumps(result).encode() if self.path == "/state" else PAGE.encode()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json" if self.path == "/state" else "text/html; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
