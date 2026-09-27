"""Run the reproducible FlashEats mock Dispatch API.

The server is independently implemented from the documented classroom API
contract. The course-provided fixture and its provenance are stored beside it.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit


FIXTURE_PATH = Path(__file__).with_name("dispatch_data.json")
with FIXTURE_PATH.open(encoding="utf-8") as handle:
    DISPATCH_RECORDS = json.load(handle)
if not isinstance(DISPATCH_RECORDS, list):
    raise RuntimeError(f"Dispatch fixture must contain a JSON list: {FIXTURE_PATH}")


class DispatchRequestHandler(BaseHTTPRequestHandler):
    """Serve the classroom Dispatch response contract."""

    page_hits: dict[int, int] = {}
    page_hits_lock = Lock()
    transient_failures = True

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send_json(
                200,
                {"status": "ok", "service": "flasheats-dispatch-api"},
            )
            return

        if parsed.path == "/dispatch/orders":
            try:
                query = parse_qs(parsed.query)
                page = max(1, int(query.get("page", ["1"])[0]))
                page_size = min(
                    200,
                    max(1, int(query.get("page_size", ["50"])[0])),
                )
            except (TypeError, ValueError):
                self._send_json(400, {"error": "page and page_size must be integers"})
                return

            with self.page_hits_lock:
                hits = self.page_hits.get(page, 0) + 1
                self.page_hits[page] = hits

            if self.transient_failures and page == 3 and hits == 1:
                self._send_json(
                    500,
                    {"error": "temporary upstream failure", "retryable": True},
                )
                return
            if self.transient_failures and page == 5 and hits == 1:
                self._send_json(
                    429,
                    {"error": "rate limit exceeded", "retry_after_seconds": 1},
                )
                return

            start = (page - 1) * page_size
            end = start + page_size
            self._send_json(
                200,
                {
                    "data": DISPATCH_RECORDS[start:end],
                    "page": page,
                    "page_size": page_size,
                    "has_more": end < len(DISPATCH_RECORDS),
                    "total_records": len(DISPATCH_RECORDS),
                },
            )
            return

        prefix = "/dispatch/orders/"
        if parsed.path.startswith(prefix):
            order_id = parsed.path[len(prefix):]
            record = next(
                (
                    item
                    for item in DISPATCH_RECORDS
                    if item.get("order_id") == order_id
                ),
                None,
            )
            if record is None:
                self._send_json(
                    404,
                    {"error": "order not found", "order_id": order_id},
                )
            else:
                self._send_json(200, record)
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the FlashEats mock Dispatch API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--no-transient-failures",
        action="store_true",
        help="Disable the one-time HTTP 500/429 teaching failures",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    DispatchRequestHandler.transient_failures = not args.no_transient_failures
    DispatchRequestHandler.page_hits = {}
    server = ThreadingHTTPServer((args.host, args.port), DispatchRequestHandler)
    print(
        f"FlashEats mock Dispatch API listening on http://{args.host}:{server.server_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
