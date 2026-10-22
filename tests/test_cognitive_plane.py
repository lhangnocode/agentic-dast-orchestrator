from orchestrator.policy import classify_surface
from orchestrator.safety_firewall import SafetyFirewall

BASE = "http://localhost/"


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
    spa = classify_surface({"hash_routes": 4, "api_like": 10, "json_responses": 6, "html_pages": 1})
    assert spa["spa_likelihood"] == "high" and set(spa["spa_signals"]) == {"hash_routes", "js_shell", "api_heavy"}
    mpa = classify_surface({"hash_routes": 0, "api_like": 0, "json_responses": 0, "html_pages": 40})
    assert mpa["spa_likelihood"] == "low"


def test_surface_profile_uses_content_type():
    import tempfile
    from normalization.normalizer import normalize_endpoint
    from storage.state_store import StateStore
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(db_path=f"{tmpdir}/s.db")
        for path, ctype in [("/", "text/html"), ("/ftp/legal.md", "text/markdown"),
                            ("/rest/languages", "application/json"), ("/#/login", "")]:
            ep = normalize_endpoint(f"http://localhost:3000{path}", source_tool="zap_client_spider",
                                    status_code=0 if "#" in path else 200)
            ep["content_type"] = ctype
            store.insert_or_update_endpoint(ep, 1)
        profile = store.get_surface_profile()
        assert profile["html_pages"] == 1          # the markdown file is not a page
        assert profile["json_responses"] == 1 and profile["hash_routes"] == 1
        assert classify_surface(profile)["spa_likelihood"] == "high"   # Juice Shop shape
