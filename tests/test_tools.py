import json

from tools.firecrawl_client import FirecrawlClient
from tools.registry import ToolRegistry
from tools.wapiti_wrapper import WapitiWrapper
from tools.zap_wrapper import ZapWrapper


def test_firecrawl_unconfigured_returns_nothing():
    client = FirecrawlClient(api_key="", target_base="http://localhost:3000/")
    assert client.is_available() is False
    assert client.run_agent_discovery(["http://localhost:3000/"]) == []


def test_firecrawl_localhost_needs_tunnel():
    client = FirecrawlClient(api_key="key", target_base="http://localhost:3000/", public_tunnel_url="")
    assert client.can_reach_target() is False
    tunneled = FirecrawlClient(api_key="key", target_base="http://localhost:3000/",
                               public_tunnel_url="https://abc.trycloudflare.com")
    assert tunneled.is_available() is True
    assert tunneled._to_cloud_url("http://localhost:3000/#/login") == "https://abc.trycloudflare.com/#/login"
    assert tunneled._from_cloud_url("https://abc.trycloudflare.com/api/x?y=1") == "http://localhost:3000/api/x?y=1"


def test_firecrawl_fallback_file_replay(tmp_path):
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps(["http://localhost:3000/#/about"]))
    client = FirecrawlClient(api_key="", fallback_routes_file=str(routes))
    results = client.run_agent_discovery(["http://localhost:3000/"])
    assert results == [{"url": "http://localhost:3000/#/about", "method": "GET",
                        "source": "firecrawl_navigate", "status_code": 0}]


def test_zap_wrapper_unreachable_safe():
    zap = ZapWrapper(api_url="http://127.0.0.1:59999", api_key="dummy")
    assert zap.is_alive() is False
    assert zap.collect_messages() == []


def test_zap_host_alias_mapping():
    zap = ZapWrapper(target_base="http://localhost:3000/", target_host_alias="host.docker.internal")
    assert zap.to_zap_url("http://localhost:3000/#/login") == "http://host.docker.internal:3000/#/login"
    assert zap.from_zap_url("http://host.docker.internal:3000/api/x") == "http://localhost:3000/api/x"
    no_alias = ZapWrapper(target_base="http://localhost:3000/")
    assert no_alias.to_zap_url("http://localhost:3000/") == "http://localhost:3000/"


def test_wapiti_wrapper_build_url():
    wapiti = WapitiWrapper()
    url = wapiti._build_url_with_params(
        base_path="http://localhost/view/channel.php",
        method="GET",
        params=[{"name": "channelName", "value1": "admin"}, {"name": "lang", "value1": "en"}]
    )
    assert "channelName=admin" in url
    assert "lang=en" in url


def test_registry_respects_disabled_tools(tmp_path):
    config = {
        "target": {"base_url": "http://localhost:3000/"},
        "tools": {
            "zap": {"api_url": "http://127.0.0.1:59999"},
            "wapiti": {"enabled": False, "session_file": str(tmp_path / "s.db"),
                       "report_file": str(tmp_path / "r.json"), "log_file": str(tmp_path / "w.log")},
            "firecrawl": {"api_key": ""},
        },
    }
    registry = ToolRegistry.from_config(config)
    assert registry.available_tools() == ["http_probe"]  # the probe needs no external service
    assert "wapiti_form_crawl" in registry.disabled


def test_zap_message_parsing_skips_placeholders_and_assets():
    zap = ZapWrapper(target_base="http://localhost:3000/", target_host_alias="host.docker.internal")
    real = zap._parse_message({"requestHeader": "GET http://host.docker.internal:3000/api/x HTTP/1.1",
                               "responseHeader": "HTTP/1.1 200 OK\r\nContent-Type: application/json"}, "zap_traditional_spider")
    assert real["url"] == "http://localhost:3000/api/x" and real["status_code"] == 200
    assert real["content_type"] == "application/json"
    placeholder = zap._parse_message({"requestHeader": "GET http://host.docker.internal:3000/rest HTTP/1.1",
                                      "responseHeader": "HTTP/1.0 0"}, "zap_traditional_spider")
    assert placeholder["status_code"] == 0


def test_wapiti_times_first_crawled_resource(tmp_path):
    import sys
    import time
    from tools.wapiti_wrapper import WapitiWrapper
    w = WapitiWrapper(session_db_path=str(tmp_path / "s.db"), report_path=str(tmp_path / "r.json"),
                      log_path=str(tmp_path / "w.log"))
    log = tmp_path / "w.log"
    log.write_text("[+] GET http://old-run/ (0)\n")          # an earlier run's line does not count
    script = (f"import time; f = open({str(log)!r}, 'a'); f.write('Wapiti 3.3.2\\n'); f.flush(); "
              "time.sleep(0.5); f.write('[+] GET http://localhost:3456/ (0)\\n'); f.flush(); time.sleep(0.3)")
    start = time.time()
    code, _ = w._run_streaming([sys.executable, "-c", script], env=None, timeout=10)
    assert code == 0 and w.last_first_request_at is not None
    assert 0.45 <= w.last_first_request_at - start < 0.75   # the banner line does not count


def test_tool_run_startup_seconds():
    from tools.registry import ToolRun
    run = ToolRun(tool="x", seeds=[], started_at=100.0, first_request_at=102.5)
    assert run.startup_seconds == 2.5
    assert ToolRun(tool="x", seeds=[], started_at=100.0).startup_seconds is None
