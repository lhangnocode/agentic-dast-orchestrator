import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from normalization.normalizer import normalize_endpoint
from storage.state_store import StateStore
from tools.http_probe import HttpProbe


class _Handler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _Handler.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/page":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "/page")
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _Handler.seen = []
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_probe_records_status_and_content_type(server):
    probe = HttpProbe(headers={"Authorization": "Bearer t"})
    results = {r["url"]: r for r in probe.run([f"{server}/page", f"{server}/old", f"{server}/missing",
                                               f"{server}/#/login"])}
    assert results[f"{server}/page"]["status_code"] == 200
    assert results[f"{server}/page"]["content_type"] == "text/html"
    assert results[f"{server}/old"]["status_code"] == 302          # redirect observed, not followed
    assert results[f"{server}/missing"]["status_code"] == 404
    assert f"{server}/#/login" not in results                        # client route: never requested
    assert probe.last_request_count == 3
    assert all(auth == "Bearer t" for _, auth in _Handler.seen)
    assert "/page" in [p for p, _ in _Handler.seen] and len(_Handler.seen) == 3


def test_probe_reports_failure_when_nothing_answers():
    probe = HttpProbe(timeout_sec=0.5)
    assert probe.run(["http://127.0.0.1:9/unreachable"]) == []
    assert probe.last_status == "failed"


def test_unknown_status_frontier_and_fingerprint():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(db_path=os.path.join(tmpdir, "s.db"))
        for url, status in [("http://localhost:3000/docs", 0), ("http://localhost:3000/#/login", 0),
                            ("http://localhost:3000/api/x", 200)]:
            store.insert_or_update_endpoint(normalize_endpoint(url, source_tool="zap_client_spider",
                                                               status_code=status), 1)
        assert store.get_unknown_status_urls() == ["http://localhost:3000/docs"]
        before = store.frontier_fingerprint("http_probe")
        assert before.startswith("1:")
        # A probe answer fills in the status; the probe frontier becomes empty
        store.insert_or_update_endpoint(normalize_endpoint("http://localhost:3000/docs", source_tool="http_probe",
                                                           status_code=200), 2)
        assert store.get_unknown_status_urls() == []
        assert store.frontier_fingerprint("http_probe") != before
