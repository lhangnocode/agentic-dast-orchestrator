"""
Logging reverse proxy for the public tunnel.

Firecrawl Cloud reaches a localhost target only through PUBLIC_TUNNEL_URL; every other tool talks to
localhost directly. Pointing the tunnel at this proxy instead of the target therefore records exactly
the requests Firecrawl's browser made, one JSON line per request:

  {"ts", "method", "path", "status", "bytes", "duration_s", "user_agent", "referer", "error"}

  cloudflared tunnel --url http://localhost:3457
  python -m tools.traffic_proxy --listen 3457 --upstream http://localhost:3456 --log traffic.jsonl

Responses are streamed (server-sent events keep working) and redirects are passed through, not followed.
The Host header is forwarded unchanged, so the target sees the same request as without the proxy.
"""

import argparse
import http.client
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("orchestrator.traffic_proxy")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade"}


class TrafficProxy:
    """Threaded reverse proxy to `upstream` that appends one JSON line per request to `log_file`."""

    def __init__(self, listen_port: int, upstream: str, log_file: str, listen_host: str = "127.0.0.1",
                 timeout_s: float = 60.0):
        up = urlparse(upstream)
        self.upstream_host, self.upstream_port = up.hostname or "localhost", up.port or 80
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.server = ThreadingHTTPServer((listen_host, listen_port), self._handler_class())
        self.server.daemon_threads = True

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def start(self) -> "TrafficProxy":
        self._thread = threading.Thread(target=self.server.serve_forever, name="traffic-proxy", daemon=True)
        self._thread.start()
        logger.info(f"[PROXY] :{self.port} -> {self.upstream_host}:{self.upstream_port}, logging to {self.log_file}")
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _handler_class(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"   # one request per connection; the body ends when we close

            def log_message(self, *args):  # silence the default stderr access log
                pass

            def _forward(self):
                started = time.time()
                record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                          "method": self.command, "path": self.path, "status": None, "bytes": 0,
                          "user_agent": self.headers.get("User-Agent"), "referer": self.headers.get("Referer"),
                          "error": None}
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length) if length else None
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
                    conn = http.client.HTTPConnection(proxy.upstream_host, proxy.upstream_port,
                                                      timeout=proxy.timeout_s)
                    conn.request(self.command, self.path, body=body, headers=headers)
                    resp = conn.getresponse()
                    record["status"] = resp.status
                    self.send_response_only(resp.status, resp.reason)
                    for k, v in resp.getheaders():
                        if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                            self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if self.command != "HEAD":
                        while True:
                            chunk = resp.read1(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            record["bytes"] += len(chunk)
                    conn.close()
                except Exception as e:   # upstream down, client gone, timeout on a stream...
                    record["error"] = f"{type(e).__name__}: {e}"
                    if record["status"] is None:
                        try:
                            self.send_error(502, "Bad Gateway")
                            record["status"] = 502
                        except Exception:
                            pass
                finally:
                    record["duration_s"] = round(time.time() - started, 3)
                    proxy.write(record)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

        return Handler


def record_time(record: dict):
    """Epoch seconds of a log record's `ts` (None if missing or malformed)."""
    try:
        return datetime.fromisoformat(record["ts"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def read_traffic(log_file: str) -> list:
    path = Path(log_file)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Logging reverse proxy (put it between the tunnel and the target).")
    parser.add_argument("--listen", type=int, required=True, help="local port the tunnel points at")
    parser.add_argument("--upstream", required=True, help="target base URL, e.g. http://localhost:3456")
    parser.add_argument("--log", required=True, help="JSONL file to append to")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    proxy = TrafficProxy(args.listen, args.upstream, args.log)
    proxy.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        proxy.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
