import os
import shutil
import tempfile
import pytest

from normalization.normalizer import (
    is_crawler_trap_or_junk,
    generalize_path,
    generalize_query,
    compute_canonical_key,
    normalize_endpoint,
)
from storage.state_store import StateStore


def test_rule1_crawler_trap_filtering():
    assert is_crawler_trap_or_junk("http://localhost/view/test.php?v=*")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/style.css")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/image.png")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/%7BvideoLink%7D")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/webSiteRootURL/api")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/a")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/var/www/html/file.php")[0] is True
    assert is_crawler_trap_or_junk("http://localhost/dir/dir/dir/test")[0] is True
    # Valid endpoint
    assert is_crawler_trap_or_junk("http://localhost/view/channel.php?channelName=admin")[0] is False


def test_rule3_path_and_query_generalization():
    assert generalize_path("/channel/admin") == "/channel/{user}"
    assert generalize_path("/channel/john_doe/roku.json") == "/channel/{user}/roku.json"
    assert generalize_path("/page/2/") == "/page/{page}/"
    assert generalize_path("/page/5") == "/page/{page}/"
    assert generalize_path("/video/123") == "/video/{id}"
    assert generalize_path("/admin") == "/admin/"

    q_str, keys = generalize_query("lang=en&channelName=admin&custom=val")
    assert keys == ("channelName", "custom", "lang")
    assert q_str == "channelName={user}&custom={custom}&lang={lang}"


def test_compute_canonical_key():
    key, hsh, path, params = compute_canonical_key("get", "/channel/admin", "lang=en")
    assert key == "GET /channel/{user}?lang={lang}"
    assert len(hsh) == 64
    assert path == "/channel/{user}"
    assert params == ("lang",)


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
