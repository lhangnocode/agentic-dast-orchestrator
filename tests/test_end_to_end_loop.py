import json
import tempfile
import threading
from pathlib import Path

from orchestrator.loop import AdaptiveOrchestrator
from tools.registry import ToolRun

BASE = "http://localhost:3000/"
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
         "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango"]

ZAP_PAGES = [f"{BASE}docs/{w}" for w in WORDS]                         # 20 pages (> any old seed cap)
WAPITI_PAGES = [f"{BASE}forms/{w}?ref=7" for w in WORDS[:18]]
CLIENT_ROUTES = [f"{BASE}#/{w}" for w in WORDS[:5]]
FIRECRAWL_LINKS = [f"{BASE}#/basket", f"{BASE}checkout"]                # status unknown (0)


def _page(url, status=200, ctype="text/html"):
    return {"url": url, "method": "GET", "status_code": status, "content_type": ctype}


FAKE_RESULTS = {
    "zap_traditional_spider": [_page(u) for u in ZAP_PAGES]
    + [_page("https://cdn.example.com/lib")],                            # out of scope
    "zap_client_spider": [_page(u, status=0, ctype="") for u in CLIENT_ROUTES]
    + [_page(f"{BASE}api/Products/1", ctype="application/json")],
    "wapiti_form_crawl": [_page(u) for u in WAPITI_PAGES],
    "firecrawl_navigate": [_page(u, status=0, ctype="") for u in FIRECRAWL_LINKS],
}


class FakeRegistry:
    """Stands in for ToolRegistry: no ZAP/Wapiti/Firecrawl needed."""

    def __init__(self, available):
        self._available = available
        self.calls = []
        self._lock = threading.Lock()
        self.firecrawl = type("FC", (), {"public_tunnel_url": ""})()

    def available_tools(self):
        return list(self._available)

    def run(self, tool, seeds, base_url, focus=""):
        with self._lock:
            self.calls.append((tool, list(seeds), focus))
        if tool == "http_probe":
            results = [_page(u) for u in seeds]
        else:
            results = [dict(r) for r in FAKE_RESULTS.get(tool, [])]
        for r in results:
            r["source"] = tool
        return ToolRun(tool=tool, seeds=list(seeds), results=results, requests_sent=len(results))

    def seeds_of(self, tool, nth=0):
        return [s for t, s, _ in self.calls if t == tool][nth]

    def tools_called(self):
        return [t for t, _, _ in self.calls]


ALL = ["zap_traditional_spider", "zap_client_spider", "wapiti_form_crawl", "firecrawl_navigate", "http_probe"]


def _config(tmp_path: Path, parallel: bool = False) -> dict:
    return {
        "target": {"base_url": BASE, "allowed_hosts": ["localhost", "127.0.0.1"]},
        "orchestrator": {"max_iterations": 30, "parallel": parallel},
        "storage": {"sqlite_db": str(tmp_path / "state.db")},
    }


def _run(monkeypatch, tmp_path, available=ALL, **kwargs):
    monkeypatch.chdir(tmp_path)  # export_results and the run log write to ./output
    registry = FakeRegistry(available)
    orch = AdaptiveOrchestrator(_config(tmp_path, **kwargs), tools=registry)
    return orch, registry, orch.run()


def test_stages_then_rules_until_saturation(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir))
        called = registry.tools_called()

        # Stage 1: ZAP traditional -> Client Spider (same lane) and Wapiti, all from the base URL
        assert called[:3] == ["zap_traditional_spider", "zap_client_spider", "wapiti_form_crawl"]
        assert all(seeds == [BASE] for _, seeds, _ in registry.calls[:3])

        # Stage 2: Firecrawl gets every page/route found so far (no cap), never the JSON API
        fc_seeds = set(registry.seeds_of("firecrawl_navigate", 0))
        assert set(ZAP_PAGES) | set(WAPITI_PAGES) | set(CLIENT_ROUTES) <= fc_seeds
        assert f"{BASE}api/Products/1" not in fc_seeds
        assert registry.calls[3][2]                                   # stage-2 focus is set

        # Rule rounds: discoveries flow both ways without a cap
        wapiti_later = set(registry.seeds_of("wapiti_form_crawl", 1))
        assert set(ZAP_PAGES) <= wapiti_later and f"{BASE}checkout" in wapiti_later
        assert set(WAPITI_PAGES) <= set(registry.seeds_of("zap_traditional_spider", 1))
        assert f"{BASE}#/basket" in set(registry.seeds_of("zap_client_spider", 1))

        # The probe checks unknown-status server URLs but never client routes
        probed = set(registry.seeds_of("http_probe", 0))
        assert f"{BASE}checkout" in probed and not any("#/" in u for u in probed)

        assert summary["termination"] == "saturation"
        assert {"stage1", "stage2", "R3_browser_spider", "R4_link_spider", "R5_form_crawler",
                "R7_probe"} <= set(summary["rule_counts"])
        assert summary["iterations_run"] < 10
        assert not any("cdn.example.com" in ep["normalized_url"] for ep in orch.store.get_all_endpoints())


def test_history_and_run_log(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir))
        last = orch.store.get_last_runs()
        assert last["http_probe"]["rule_id"] == "R7_probe"
        assert all(row["status"] == "ok" and row["frontier_fingerprint"] for row in last.values())
        events = [json.loads(l) for l in Path("output/logs/run_events.jsonl").read_text().splitlines()]
        kinds = [e["event"] for e in events]
        assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
        assert kinds.count("tool_run") == len(registry.calls)
        assert {e["rule_id"] for e in events if e["event"] == "decision"} >= {"R4_link_spider", "R7_probe"}
        assert {e["run_id"] for e in events} == {summary["run_id"]}


def test_parallel_lanes_keep_zap_tools_sequential(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir), parallel=True)
        first_stage = registry.tools_called()[:3]
        assert first_stage.index("zap_traditional_spider") < first_stage.index("zap_client_spider")
        assert summary["termination"] == "saturation"


def test_runs_without_firecrawl(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir),
                                       available=[t for t in ALL if t != "firecrawl_navigate"])
        assert "firecrawl_navigate" not in registry.tools_called()
        assert summary["termination"] == "saturation" and "stage2" not in summary["rule_counts"]


def test_no_tools_available_terminates_cleanly(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir), available=[])
        assert summary["total_canonical_endpoints"] == 1  # only the seeded base URL
        assert summary["termination"] == "saturation" and summary["iterations_run"] == 0
