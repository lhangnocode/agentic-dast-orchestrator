import tempfile

from llm.qwen_client import QwenClient
from normalization.normalizer import normalize_endpoint
from orchestrator.compactor import StateCompactor
from orchestrator.decision_schema import OrchestratorDecision, ToolActionEnum
from orchestrator.policy import HeuristicPolicy, classify_surface
from orchestrator.safety_firewall import SafetyFirewall
from storage.state_store import StateStore

BASE = "http://localhost/"
ALL = ["zap_traditional_spider", "zap_ajax_spider", "wapiti_form_crawl", "firecrawl_navigate"]


def _store(tmpdir, urls):
    store = StateStore(db_path=f"{tmpdir}/state.db")
    for url, status, tool in urls:
        store.insert_or_update_endpoint(normalize_endpoint(url, source_tool=tool, status_code=status), 1)
    return store


def _record(store, tool):
    store.record_iteration({"iteration": 1, "chosen_tool": tool, "endpoints_before": 0,
                            "endpoints_after": 0, "new_endpoints_discovered": 0, "marginal_gain": 0.0})


def test_decision_schema_validation():
    decision = OrchestratorDecision(
        action=ToolActionEnum.FIRECRAWL_NAVIGATE,
        reasoning="Test reasoning",
        target_seeds=["http://localhost/admin"],
        focus="open the account menu",
    )
    assert decision.action == ToolActionEnum.FIRECRAWL_NAVIGATE
    assert decision.focus == "open the account menu"


def test_safety_firewall():
    firewall = SafetyFirewall(allowed_hosts=["localhost", "127.0.0.1", "host.docker.internal"], base_url=BASE)
    assert firewall.is_in_scope("http://localhost/admin") is True
    assert firewall.is_in_scope("http://127.0.0.1:8080/api") is True
    assert firewall.is_in_scope("http://169.254.169.254/latest/meta-data") is False
    assert firewall.is_in_scope("http://malicious.com/attack") is False
    assert firewall.is_in_scope("http://localhost/logout") is False
    assert firewall.sanitize_seeds(["http://malicious.com", "http://localhost/logout", "http://localhost/view"]) == ["http://localhost/view"]
    assert firewall.sanitize_seeds(["http://evil.com"]) == [BASE]


def test_classify_surface_detects_spa_signals():
    spa = classify_surface({"hash_routes": 4, "api_like": 10, "html_pages": 1})
    assert spa["spa_likelihood"] == "high"
    mpa = classify_surface({"hash_routes": 0, "api_like": 0, "html_pages": 40})
    assert mpa["spa_likelihood"] == "low"


def test_policy_prefers_ajax_spider_for_spa():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, [
            (f"{BASE}", 200, "zap_traditional_spider"),
            (f"{BASE}api/items/1", 200, "zap_traditional_spider"),
            (f"{BASE}api/users", 200, "zap_traditional_spider"),
            (f"{BASE}api/orders", 200, "wapiti_form_crawl"),
        ])
        _record(store, "zap_traditional_spider")
        _record(store, "wapiti_form_crawl")
        d = HeuristicPolicy().decide(store, ALL, BASE)
        assert d.action == ToolActionEnum.ZAP_AJAX_SPIDER
        assert d.target_seeds[0] == BASE


def test_policy_targets_firecrawl_on_auth_walls():
    with tempfile.TemporaryDirectory() as tmpdir:
        pages = [(f"{BASE}page{i}", 200, "zap_traditional_spider") for i in range(6)]
        store = _store(tmpdir, pages + [(f"{BASE}admin", 302, "zap_traditional_spider")])
        for tool in ("zap_traditional_spider", "wapiti_form_crawl", "zap_ajax_spider"):
            _record(store, tool)
        # Classic multi-page site, ajax already ran once -> walled page goes to Firecrawl
        d = HeuristicPolicy().decide(store, ALL, BASE)
        assert d.action == ToolActionEnum.FIRECRAWL_NAVIGATE
        assert d.target_seeds == [f"{BASE}admin"]
        assert d.focus


def test_policy_skips_unavailable_tools_and_terminates():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, [(f"{BASE}", 200, "zap_traditional_spider")])
        d = HeuristicPolicy().decide(store, [], BASE)
        assert d.action == ToolActionEnum.TERMINATE


def test_compactor_context_and_unconfigured_llm():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, [(f"{BASE}#/login", 200, "zap_ajax_spider")])
        context_yaml = StateCompactor().build_prompt_context(
            store=store, current_iteration=2, max_iterations=5, base_url=BASE,
            available_tools=["zap_ajax_spider"],
        )
        assert "available_tools" in context_yaml
        assert "spa_likelihood" in context_yaml
        assert "hash_routes: 1" in context_yaml

        client = QwenClient(api_key="")
        assert client.is_configured() is False
        assert client.get_decision(context_yaml) is None
