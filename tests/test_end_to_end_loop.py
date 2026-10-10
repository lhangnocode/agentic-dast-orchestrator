import tempfile
import threading
from pathlib import Path

from orchestrator.loop import AdaptiveOrchestrator
from tools.registry import ToolRun

BASE = "http://localhost:3000/"
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
         "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango"]

# What each fake tool "discovers" (more than 15 URLs each, to prove there is no seed cap)
ZAP_URLS = [f"{BASE}docs/{w}" for w in WORDS]
WAPITI_URLS = [f"{BASE}forms/{w}?ref=7" for w in WORDS[:18]]
AJAX_URLS = [f"{BASE}api/Products/{i}" for i in range(1, 4)] + [f"{BASE}rest/user/whoami"]

FAKE_RESULTS = {
    "zap_traditional_spider": [{"url": u, "method": "GET", "status_code": 200} for u in ZAP_URLS]
    + [{"url": "https://cdn.example.com/lib", "method": "GET", "status_code": 200}],  # out of scope
    "zap_ajax_spider": [{"url": u, "method": "GET", "status_code": 200} for u in AJAX_URLS],
    "wapiti_form_crawl": [{"url": u, "method": "GET", "status_code": 200} for u in WAPITI_URLS]
    + [{"url": f"{BASE}api/Feedbacks", "method": "POST", "status_code": 201}],
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
            self.calls.append((tool, list(seeds)))
        results = [dict(r, source=tool) for r in FAKE_RESULTS.get(tool, [])]
        return ToolRun(tool=tool, seeds=list(seeds), results=results, requests_sent=len(results))

    def seeds_of(self, tool, nth):
        return [s for t, s in self.calls if t == tool][nth]


ALL_CLASSIC = ["zap_traditional_spider", "zap_ajax_spider", "wapiti_form_crawl"]


def _config(tmp_path: Path, parallel: bool = False, exchange: bool = True) -> dict:
    return {
        "target": {"base_url": BASE, "allowed_hosts": ["localhost", "127.0.0.1"]},
        "orchestrator": {
            "max_iterations": 6,
            "marginal_gain_threshold": 0.01,
            "max_runs_per_tool": 3,
            "seed_limit": None,
            "bootstrap": {"tools": ALL_CLASSIC, "parallel": parallel, "exchange_round": exchange},
        },
        "llm": {"api_key": ""},
        "storage": {"sqlite_db": str(tmp_path / "state.db")},
    }


def _run(monkeypatch, tmp_path, **kwargs):
    monkeypatch.chdir(tmp_path)  # export_results writes to ./output
    registry = FakeRegistry(ALL_CLASSIC)
    orch = AdaptiveOrchestrator(_config(tmp_path, **kwargs), tools=registry)
    return orch, registry, orch.run()


def test_round1_then_full_exchange_without_seed_limit(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir))
        tools_called = [t for t, _ in registry.calls]

        # Round 1: all classic tools from the base URL only
        assert tools_called[:3] == ALL_CLASSIC
        assert all(seeds == [BASE] for _, seeds in registry.calls[:3])

        # Round 2: ZAP tools get ALL of Wapiti's URLs (concrete, not {placeholder} templates)
        expected_from_wapiti = set(WAPITI_URLS)
        assert set(registry.seeds_of("zap_traditional_spider", 1)) == expected_from_wapiti
        assert set(registry.seeds_of("zap_ajax_spider", 1)) == expected_from_wapiti
        assert len(expected_from_wapiti) > 15

        # ...and Wapiti gets ALL of ZAP's (traditional + ajax) URLs
        wapiti_seeds = set(registry.seeds_of("wapiti_form_crawl", 1))
        # (/api/Products/1..3 are one canonical endpoint -> one representative seed)
        assert set(ZAP_URLS) <= wapiti_seeds and f"{BASE}rest/user/whoami" in wapiti_seeds
        assert len([s for s in wapiti_seeds if "/api/Products/" in s]) == 1
        assert len(wapiti_seeds) == len(ZAP_URLS) + 2
        assert not any("{" in s for s in wapiti_seeds)
        assert not any("cdn.example.com" in s for s in wapiti_seeds)

        assert summary["decision_sources"]["bootstrap"] == 3
        assert summary["decision_sources"]["exchange"] == 3
        assert Path(summary["output_json"]).exists()


def test_parallel_lanes_keep_zap_tools_sequential(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir), parallel=True)
        tools_called = [t for t, _ in registry.calls]
        # Same engine never overlaps: traditional always precedes ajax within each round
        first_round = tools_called[:3]
        assert first_round.index("zap_traditional_spider") < first_round.index("zap_ajax_spider")
        assert summary["decision_sources"]["exchange"] == 3


def test_exchange_round_can_be_disabled(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        orch, registry, summary = _run(monkeypatch, Path(tmpdir), exchange=False)
        assert summary["decision_sources"]["exchange"] == 0


def test_no_tools_available_terminates_cleanly(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        monkeypatch.chdir(tmp_path)
        orch = AdaptiveOrchestrator(_config(tmp_path), tools=FakeRegistry([]))
        summary = orch.run()
        assert summary["total_canonical_endpoints"] == 1  # only the seeded base URL
        assert summary["decision_sources"]["heuristic"] == 1
