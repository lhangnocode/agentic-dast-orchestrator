import pytest
from tools.firecrawl_client import FirecrawlClient
from tools.wapiti_wrapper import WapitiWrapper
from tools.zap_wrapper import ZapWrapper


def test_firecrawl_fallback_routes():
    client = FirecrawlClient(api_key="", mock_fallback=True)
    routes = client.run_agent_discovery("http://localhost/")
    assert len(routes) > 5
    assert any("siteConfigurations" in r["url"] for r in routes)
    assert all(r["source"] == "firecrawl" for r in routes)


def test_zap_wrapper_unreachable_safe():
    zap = ZapWrapper(api_url="http://127.0.0.1:59999", api_key="dummy")
    assert zap.is_alive() is False
    msgs = zap.collect_messages()
    assert msgs == []


def test_wapiti_wrapper_build_url():
    wapiti = WapitiWrapper()
    url = wapiti._build_url_with_params(
        base_path="http://localhost/view/channel.php",
        method="GET",
        params=[{"name": "channelName", "value1": "admin"}, {"name": "lang", "value1": "en"}]
    )
    assert "channelName=admin" in url
    assert "lang=en" in url
