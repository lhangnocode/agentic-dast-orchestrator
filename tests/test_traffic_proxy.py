import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from tools.traffic_proxy import TrafficProxy, read_traffic


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/score/x")
            self.end_headers()
            return
        if self.path == "/stream":   # SSE-like: no length, data in pieces, then close
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"data: {i}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            return
        body = f"host={self.headers.get('Host')} path={self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(201)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_proxy_forwards_and_logs_every_request(upstream, tmp_path):
    log = tmp_path / "traffic.jsonl"
    with TrafficProxy(0, upstream, str(log)) as proxy:
        base = f"http://127.0.0.1:{proxy.port}"
        r = requests.get(f"{base}/score/a?colour=green", headers={"Host": "abc.trycloudflare.com",
                                                                   "User-Agent": "agent-ua"})
        assert r.status_code == 200 and r.text == "host=abc.trycloudflare.com path=/score/a?colour=green"
        assert requests.post(f"{base}/form", data=b"name=x").text == "name=x"
        redirect = requests.get(f"{base}/redirect", allow_redirects=False)
        assert redirect.status_code == 302 and redirect.headers["Location"] == "/score/x"   # passed through
        assert requests.get(f"{base}/stream").text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"

    traffic = read_traffic(str(log))
    assert [(t["method"], t["path"], t["status"]) for t in traffic] == [
        ("GET", "/score/a?colour=green", 200), ("POST", "/form", 201), ("GET", "/redirect", 302),
        ("GET", "/stream", 200)]
    assert traffic[0]["user_agent"] == "agent-ua" and traffic[3]["bytes"] == 27
    assert all(t["error"] is None and t["duration_s"] >= 0 for t in traffic)


def test_proxy_logs_upstream_failure(tmp_path):
    log = tmp_path / "traffic.jsonl"
    with TrafficProxy(0, "http://127.0.0.1:9", str(log)) as proxy:   # nothing listens on port 9
        assert requests.get(f"http://127.0.0.1:{proxy.port}/x").status_code == 502
    (record,) = read_traffic(str(log))
    assert record["status"] == 502 and record["error"]
