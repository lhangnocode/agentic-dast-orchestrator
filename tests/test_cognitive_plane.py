import tempfile
import pytest

from llm.qwen_client import QwenClient
from orchestrator.compactor import StateCompactor
from orchestrator.decision_schema import OrchestratorDecision, ToolActionEnum
from orchestrator.safety_firewall import SafetyFirewall
from storage.state_store import StateStore


def test_decision_schema_validation():
    decision = OrchestratorDecision(
        action=ToolActionEnum.FIRECRAWL_NAVIGATE,
        reasoning="Test reasoning",
        target_seeds=["http://localhost/admin"],
        expected_discovery_type="admin_panels"
    )
    assert decision.action == ToolActionEnum.FIRECRAWL_NAVIGATE
    assert len(decision.target_seeds) == 1


def test_safety_firewall():
    firewall = SafetyFirewall(
        allowed_hosts=["localhost", "127.0.0.1", "host.docker.internal"],
        base_url="http://localhost/"
    )

    # Valid
    assert firewall.is_in_scope("http://localhost/admin") is True
    assert firewall.is_in_scope("http://127.0.0.1:8080/api") is True

    # Blocked external / SSRF
    assert firewall.is_in_scope("http://169.254.169.254/latest/meta-data") is False
    assert firewall.is_in_scope("http://malicious.com/attack") is False

    # Blocked logout traps
    assert firewall.is_in_scope("http://localhost/logout") is False
    assert firewall.is_in_scope("http://localhost/user/logout") is False

    # Seed sanitization
    sanitized = firewall.sanitize_seeds([
        "http://malicious.com",
        "http://localhost/logout",
        "http://localhost/view/channel.php"
    ])
    assert sanitized == ["http://localhost/view/channel.php"]

    # All blocked fallback to base_url
    empty_sanitized = firewall.sanitize_seeds(["http://evil.com"])
    assert empty_sanitized == ["http://localhost/"]


def test_compactor_and_heuristic_decision():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/state.db"
        store = StateStore(db_path=db_path)
        compactor = StateCompactor()
        context_yaml = compactor.build_prompt_context(
            store=store,
            current_iteration=1,
            max_iterations=5,
            base_url="http://localhost/"
        )
        assert "DAST Adaptive Attack Surface" in context_yaml
        assert "http://localhost/" in context_yaml

        # Check heuristic decisions
        client = QwenClient(api_key="")
        d1, _, _ = client.get_decision(context_yaml, store, current_iteration=1, base_url="http://localhost/")
        assert d1.action == ToolActionEnum.FIRECRAWL_NAVIGATE

        d2, _, _ = client.get_decision(context_yaml, store, current_iteration=2, base_url="http://localhost/")
        assert d2.action == ToolActionEnum.ZAP_TRADITIONAL_SPIDER

        d3, _, _ = client.get_decision(context_yaml, store, current_iteration=3, base_url="http://localhost/")
        assert d3.action == ToolActionEnum.WAPITI_FORM_CRAWL

        d4, _, _ = client.get_decision(context_yaml, store, current_iteration=4, base_url="http://localhost/")
        assert d4.action == ToolActionEnum.ZAP_AJAX_SPIDER

        d5, _, _ = client.get_decision(context_yaml, store, current_iteration=5, base_url="http://localhost/")
        assert d5.action == ToolActionEnum.TERMINATE
