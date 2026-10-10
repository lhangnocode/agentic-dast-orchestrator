import os
import shutil
import tempfile
import pytest

from normalization.normalizer import (
    NormalizationProfile,
    is_crawler_trap_or_junk,
    generalize_path,
    generalize_query,
    compute_canonical_key,
    normalize_endpoint,
)
from storage.state_store import StateStore


AVIDEO_LIKE_PROFILE = NormalizationProfile.from_config({
    "target": {"base_url": "http://localhost/"},
    "normalization": {
        "path_rules": [
            {"pattern": "^/channel/[^/]+$", "replacement": "/channel/{user}"},
            {"pattern": "^/page/\\d+$", "replacement": "/page/{page}/"},
        ],
        "query_aliases": {"channelName": "{user}"},
        "trailing_slash_dirs": ["/admin"],
        "junk_substrings": ["webSiteRootURL"],
    },
})


def test_rule1_crawler_trap_filtering():
    assert is_crawler_trap_or_junk("http://localhost/view/test.php?v=*")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/style.css")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/image.png")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/%7BvideoLink%7D")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/a")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/var/www/html/file.php")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/dir/dir/dir/test")[0] is True
    assert is_crawler_trap_or_junk("javascript:void(0)")[0] is True
    assert is_crawler_trap_or_junk("http://localhost:3000/juice-shop/build/routes/fileServer.js:53:13")[0] is True
    assert is_crawler_trap_or_junk("http://localhost:3000/ftp/legal.md")[0] is False
    # Target-specific junk only applies when the profile declares it
    assert is_crawler_trap_or_junk("http://localhost/webSiteRootURL/api")[0] is False
    assert is_crawler_trap_or_junk("http://localhost/webSiteRootURL/api", AVIDEO_LIKE_PROFILE)[0] is True
    assert is_crawler_trap_or_junk("http://localhost/view/channel.php?channelName=admin")[0] is False


def test_rule3_generic_path_generalization():
    assert generalize_path("/video/123") == "/video/{id}"
    assert generalize_path("/products/42.html") == "/products/{id}.html"
    assert generalize_path("/api/Users/550e8400-e29b-41d4-a716-446655440000/cart") == "/api/Users/{id}/cart"
    assert generalize_path("/rest/basket/undefined") == "/rest/basket/{id}"
    assert generalize_path("/files/d41d8cd98f00b204e9800998ecf8427e") == "/files/{hash}"
    assert generalize_path("/users/:userId") == "/users/{id}"
    assert generalize_path("//admin//") == "/admin"
    assert generalize_path("/channel/admin") == "/channel/admin"  # no site rule by default


def test_rule3_target_rules_from_config():
    assert generalize_path("/channel/admin", AVIDEO_LIKE_PROFILE) == "/channel/{user}"
    assert generalize_path("/page/5", AVIDEO_LIKE_PROFILE) == "/page/{page}/"
    assert generalize_path("/admin", AVIDEO_LIKE_PROFILE) == "/admin/"

    q_str, keys = generalize_query("lang=en&channelName=admin&custom=val", AVIDEO_LIKE_PROFILE)
    assert keys == ("channelName", "custom", "lang")
    assert q_str == "channelName={user}&custom={custom}&lang={lang}"


def test_compute_canonical_key():
    key, hsh, path, params = compute_canonical_key("get", "/channel/admin", "lang=en", AVIDEO_LIKE_PROFILE)
    assert key == "GET /channel/{user}?lang={lang}"
    assert len(hsh) == 64
    assert path == "/channel/{user}"
    assert params == ("lang",)


def test_rule4_spa_hash_routes_and_scope():
    profile = NormalizationProfile.from_config({"target": {"base_url": "http://localhost:3000/"}})
    ep = normalize_endpoint("http://127.0.0.1:3000/#/product/42?tab=reviews", profile=profile)
    assert ep["canonical_key"] == "GET #/product/{id}?tab={tab}"
    assert ep["normalized_url"] == "http://localhost:3000/#/product/{id}?tab={tab}"
    hashbang = normalize_endpoint("http://localhost:3000/#!/product/7", profile=profile)
    assert hashbang["canonical_key"] == ep["canonical_key"].split("?")[0]
    # A plain in-page anchor is not a route
    assert normalize_endpoint("http://localhost:3000/about#team", profile=profile)["canonical_key"] == "GET /about"
    # Other hosts never enter the attack surface
    assert normalize_endpoint("https://cdn.example.com/api/x", profile=profile) is None
    # Relative URLs (e.g. extracted from JS) are treated as the target's
    assert normalize_endpoint("/api/Products/3", profile=profile)["normalized_url"] == "http://localhost:3000/api/Products/{id}"


def test_state_store_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_state.db")
        store = StateStore(db_path=db_path)

        ep1 = normalize_endpoint(
            "http://localhost/view/channel.php?channelName=admin&lang=en",
            method="GET",
            source_tool="zap_traditional_spider"
        )
        assert ep1 is not None

        # Insert first time -> True
        is_new = store.insert_or_update_endpoint(ep1, current_iteration=1)
        assert is_new is True

        # Insert second time from wapiti -> False
        ep1_wapiti = normalize_endpoint(
            "http://localhost/view/channel.php?channelName=user2&lang=fr",
            method="GET",
            source_tool="wapiti_form_crawl"
        )
        is_new_again = store.insert_or_update_endpoint(ep1_wapiti, current_iteration=2)
        assert is_new_again is False

        assert store.get_total_endpoints_count() == 1
        summary = store.get_compact_summary()
        assert summary["total_canonical_endpoints"] == 1
        assert "zap_traditional_spider" in summary["tool_provenance_contributions"]
        assert "wapiti_form_crawl" in summary["tool_provenance_contributions"]


def test_frontier_returns_concrete_urls_and_filters_sources():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(db_path=os.path.join(tmpdir, "s.db"))
        for url, tool in [("http://localhost/api/Products/7", "wapiti_form_crawl"),
                          ("http://localhost/docs/intro", "zap_traditional_spider")]:
            store.insert_or_update_endpoint(normalize_endpoint(url, source_tool=tool), 1)

        frontier = store.get_pending_frontier("zap_ajax_spider", limit=None)
        assert "http://localhost/api/Products/7" in frontier        # real URL, not .../{id}
        assert not any("{" in u for u in frontier)

        only_wapiti = store.get_pending_frontier("zap_ajax_spider", limit=None, sources=["wapiti_form_crawl"])
        assert only_wapiti == ["http://localhost/api/Products/7"]
