"""P0a: raw-data capture and link graph (Client Map tree, ZAP Referer, Wapiti referer/depth, endpoint_links)."""
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from normalization.normalizer import normalize_endpoint
from orchestrator.loop import AdaptiveOrchestrator
from storage.state_store import StateStore
from tools.client_map import client_map_links, parse_client_map, parse_client_map_tree
from tools.registry import ToolRun
from tools.wapiti_wrapper import WapitiWrapper
from tools.zap_wrapper import ZapWrapper

FIXTURES = Path(__file__).parent / "fixtures"
V034 = FIXTURES / "client_map_crawl_maze_v034.yaml"     # client add-on 0.34, incl. its invalid-YAML region
V033 = FIXTURES / "client_map_juice_shop.yaml"          # client add-on 0.33 layout
ALIAS = "http://host.docker.internal:8090"


def _nodes(path):
    return parse_client_map_tree(path.read_text().splitlines(keepends=True))


# ---------------------------------------------------------------------------- G1: Client Map tree
@pytest.mark.parametrize("fixture", [V034, V033])
def test_tree_parser_finds_the_same_urls_as_the_url_parser(fixture):
    lines = fixture.read_text().splitlines(keepends=True)
    nodes = parse_client_map_tree(lines)
    urls = {n.url for n in nodes if "://" in n.url}
    urls |= {c["href"] for n in nodes for c in n.components if "://" in str(c.get("href", ""))}
    assert urls == set(parse_client_map(lines))


def test_tree_keeps_parent_visited_redirect_and_components():
    by_url = {n.url: n for n in _nodes(V034)}
    form = by_url[f"{ALIAS}/html/body/form/"]
    assert form.parent_url == f"{ALIAS}/html/body/form"
    assert form.visited is None                                   # flag absent on page nodes
    assert by_url[f"{ALIAS}/html/body/form"].visited is False     # path-only node
    assert [c["tag"] for c in form.components] == ["A", "A", "A"]
    assert form.components[0] == {"node_type": "Node Added", "href": f"{ALIAS}/html/body/form/action-get.html",
                                  "text": "action-get.html", "element_id": "", "tag": "A", "form_id": 0}
    assert by_url[f"{ALIAS}/html/body/form/action-get.html"].redirect == f"{ALIAS}/html/body/form/action-get.html/"
    get_page = by_url[f"{ALIAS}/html/body/form/action-get.html/"]
    assert {c["tag"] for c in get_page.components} == {"FORM", "INPUT"}
    assert next(c for c in get_page.components if c["tag"] == "INPUT")["tag_type"] == "text"


def test_components_in_the_invalid_yaml_region_belong_to_the_right_page():
    # The export writes an empty `components:` followed by a `children:` block indented under it
    text = V034.read_text()
    assert 'components:\n                children:' in text
    page = next(n for n in _nodes(V034) if n.url == f"{ALIAS}/html/body/form/button/formaction.html/")
    assert page.parent_url == f"{ALIAS}/html/body/form/button/formaction.html"
    assert [(c["tag"], c["href"]) for c in page.components] == [
        ("BUTTON", f"{ALIAS}/html/body/form/button/formaction.html/?"),
        ("FORM", f"{ALIAS}/html/body/form/button/formaction.html/")]


def test_client_map_links_are_dom_components_and_redirects_only():
    links = client_map_links(_nodes(V034))
    kinds = Counter(link["kind"] for link in links)
    assert kinds == {"dom:a": 4, "dom:form": 3, "dom:input": 2, "dom:button": 1, "redirect": 3}
    assert {"parent_url": f"{ALIAS}/html/body/form/button/formaction.html/",
            "child_url": f"{ALIAS}/html/body/form/button/formaction.html/?", "kind": "dom:button"} in links
    # tree parent/child pairs are path structure, not navigation
    assert not any(l["parent_url"] == f"{ALIAS}/html/body" for l in links)


def test_old_layout_hash_routes_become_untagged_edges_from_the_spa_page():
    links = client_map_links(_nodes(V033))
    about = [l for l in links if l["child_url"] == "http://host.docker.internal:3000/#/about"]
    assert about and about[0]["parent_url"] == "http://host.docker.internal:3000/#"
    assert about[0]["kind"] == "dom:unknown"                      # 0.33 components carry no tagName


# ---------------------------------------------------------------------------- G2: ZAP Referer
class _FakeCore:
    version = "2.17.0"

    def __init__(self, messages):
        self._messages = messages

    def messages(self, base, start, count):
        return self._messages[int(start):int(start) + int(count)]


def _msg(url, referer=None, status=200):
    header = f"GET {url} HTTP/1.1\r\nHost: host.docker.internal:8090"
    if referer:
        header += f"\r\nReferer: {referer}"
    return {"requestHeader": header, "responseHeader": f"HTTP/1.1 {status} OK\r\nContent-Type: text/html"}


def test_zap_referer_is_parsed_and_mapped_back_to_the_target_host():
    zap = ZapWrapper(target_base="http://localhost:8090/", target_host_alias="host.docker.internal")
    item = zap._parse_message(_msg(f"{ALIAS}/a.html", referer=f"{ALIAS}/index.html"), "zap_traditional_spider")
    assert item["referer"] == "http://localhost:8090/index.html"
    assert zap._parse_message(_msg(f"{ALIAS}/a.html"), "zap_traditional_spider")["referer"] == ""


def test_zap_collect_messages_records_referer_edges():
    zap = ZapWrapper(target_base="http://localhost:8090/", target_host_alias="host.docker.internal")
    zap._client = type("C", (), {"core": _FakeCore([
        _msg(f"{ALIAS}/"),
        _msg(f"{ALIAS}/a.html", referer=f"{ALIAS}/"),
        _msg(f"{ALIAS}/app.js", referer=f"{ALIAS}/a.html"),       # static asset: neither result nor edge
    ])})()
    zap.last_links = []
    results = zap.collect_messages(source="zap_traditional_spider")
    assert [r["url"] for r in results] == ["http://localhost:8090/", "http://localhost:8090/a.html"]
    assert zap.last_links == [{"parent_url": "http://localhost:8090/", "child_url": "http://localhost:8090/a.html",
                               "method": "GET", "kind": "referer"}]


def test_read_client_map_returns_edges_and_names_the_export_for_copying(tmp_path):
    zap = ZapWrapper(target_base="http://localhost:8090/", target_host_alias="host.docker.internal",
                     client_map_export={"container_dir": "/zap/wrk", "host_dir": str(tmp_path),
                                        "file_name": "client_map.yaml"})

    class _Client:
        @staticmethod
        def export_client_map(path):
            shutil.copy(V034, tmp_path / "client_map.yaml")

    zap._client = type("C", (), {"client": _Client()})()
    zap.last_links, zap.last_artifacts = [], {}
    results = zap.read_client_map()
    assert results and all(r["url"].startswith("http://localhost:8090") for r in results)
    assert zap.last_artifacts == {"client_map.yaml": str(tmp_path / "client_map.yaml")}
    assert len(zap.last_links) == 13
    assert all(l["parent_url"].startswith("http://localhost:8090") and l["child_url"].startswith("http://localhost:8090")
               for l in zap.last_links)


# ---------------------------------------------------------------------------- Wapiti referer/depth
def _wapiti_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE responses (response_id INTEGER PRIMARY KEY, url TEXT, status_code INTEGER, headers BLOB, body BLOB);
        CREATE TABLE paths (path_id INTEGER PRIMARY KEY, path TEXT, method TEXT, enctype TEXT, depth INTEGER,
                            encoding TEXT, headers BLOB, referer TEXT, evil BOOLEAN, response_id INTEGER);
        CREATE TABLE params (param_id INTEGER PRIMARY KEY, path_id INTEGER, type TEXT, position INTEGER,
                             name TEXT, value1 TEXT, value2 TEXT, meta TEXT);
        INSERT INTO responses VALUES (1, 'http://localhost:8090/', 200, '', ''), (2, 'http://localhost:8090/f', 200, '', '');
        INSERT INTO paths VALUES (1, 'http://localhost:8090/', 'GET', '', 0, NULL, NULL, '', 0, 1),
                                 (2, 'http://localhost:8090/f', 'POST', 'application/x-www-form-urlencoded', 1,
                                  NULL, NULL, 'http://localhost:8090/', 0, 2);
        INSERT INTO params VALUES (1, 2, 'post', 0, 'q', 'x', NULL, NULL);
    """)
    conn.commit()
    conn.close()


def test_wapiti_referer_and_depth_become_edges_and_the_db_is_named_for_copying(tmp_path):
    db = tmp_path / "session.db"
    _wapiti_db(db)
    w = WapitiWrapper(session_db_path=str(db), report_path=str(tmp_path / "r.json"), log_path=str(tmp_path / "w.log"))
    w.last_links, w.last_artifacts = [], {}
    results = w.extract_endpoints_from_db()
    assert {r["method"] for r in results} == {"GET", "POST"}
    assert w.last_links == [{"parent_url": "http://localhost:8090/", "child_url": "http://localhost:8090/f",
                             "method": "POST", "kind": "referer", "depth": 1}]
    assert w.last_artifacts == {"session.db": str(db)}


# ---------------------------------------------------------------------------- G3: endpoint_links
def _link(parent, child, tool="zap_traditional_spider", kind="referer", depth=None):
    p, c = normalize_endpoint(parent), normalize_endpoint(child)
    return {"parent_hash": p["canonical_hash"], "child_hash": c["canonical_hash"], "tool": tool, "kind": kind,
            "parent_url": parent, "child_url": child, "depth": depth}


def test_store_links_are_unique_indexed_and_keep_their_first_iteration(tmp_path):
    store = StateStore(db_path=str(tmp_path / "s.db"))
    edge = _link("http://localhost:3000/", "http://localhost:3000/#/about", kind="dom:a")
    assert store.add_links([edge], iteration=2) == 1
    assert store.add_links([edge, _link("http://localhost:3000/", "http://localhost:3000/docs")], iteration=5) == 1
    rows = store.get_links(parent_hash=edge["parent_hash"])
    assert len(rows) == 2 and rows[0]["first_seen_iteration"] == 2
    assert store.get_links(child_hash=edge["child_hash"])[0]["child_url"] == "http://localhost:3000/#/about"
    assert store.get_links(kind="dom:a", tool="zap_traditional_spider") == rows[:1]
    with sqlite3.connect(tmp_path / "s.db") as conn:
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_links_parent", "idx_links_child"} <= indexes


def test_existing_database_gets_the_link_table(tmp_path):
    path = tmp_path / "old.db"
    StateStore(db_path=str(path))
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE endpoint_links")
    assert StateStore(db_path=str(path)).add_links([_link("http://localhost:3000/", "http://localhost:3000/contact")], 1) == 1


# ---------------------------------------------------------------------------- loop: ingest + artifacts
class _LinkRegistry:
    """One Client Spider run that reports links and a raw file; nothing else is available."""

    def __init__(self, raw_file):
        self.raw_file = raw_file
        self.firecrawl = type("FC", (), {"public_tunnel_url": ""})()

    def available_tools(self):
        return ["zap_client_spider"]

    def run(self, tool, seeds, base_url, focus=""):
        base = "http://localhost:3000/"
        return ToolRun(tool=tool, seeds=list(seeds), results=[
            {"url": base, "method": "GET", "status_code": 200, "content_type": "text/html", "source": tool}],
            links=[{"parent_url": base, "child_url": f"{base}#/about", "kind": "dom:a"},
                   {"parent_url": base, "child_url": f"{base}#/contact", "kind": "dom:a"},
                   {"parent_url": base, "child_url": "https://cdn.example.com/x", "kind": "dom:a"},  # other host
                   {"parent_url": base, "child_url": base, "kind": "referer"},                    # self-loop
                   {"parent_url": base, "child_url": f"{base}api/x", "method": "POST", "kind": "referer"}],
            artifacts={"client_map.yaml": str(self.raw_file)})


def test_loop_stores_normalized_edges_and_copies_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = tmp_path / "client_map.yaml"
    raw.write_text("- node: \"ClientMap\"\n")
    config = {"target": {"base_url": "http://localhost:3000/", "allowed_hosts": ["localhost"]},
              "orchestrator": {"max_iterations": 1, "stages": [["zap_client_spider"]]},
              "storage": {"sqlite_db": str(tmp_path / "state.db")},
              "logging": {"tool_runs_dir": str(tmp_path / "tool_runs"), "artifacts_dir": str(tmp_path / "artifacts")}}
    orch = AdaptiveOrchestrator(config, tools=_LinkRegistry(raw))
    orch.run()

    links = orch.store.get_links()
    assert sorted((l["kind"], l["child_url"]) for l in links) == [
        ("dom:a", "http://localhost:3000/#/about"), ("dom:a", "http://localhost:3000/#/contact"),
        ("referer", "http://localhost:3000/api/x")]
    routes = {l["child_hash"] for l in links if l["kind"] == "dom:a"}
    assert len(routes) == 2                                       # each #/ route has its own hash
    post = next(l for l in links if l["kind"] == "referer")
    assert post["child_hash"] == normalize_endpoint("http://localhost:3000/api/x", method="POST")["canonical_hash"]
    assert all(l["tool"] == "zap_client_spider" and l["first_seen_iteration"] == 1 for l in links)

    copied = tmp_path / "artifacts" / "01-zap_client_spider-client_map.yaml"
    assert copied.read_text() == raw.read_text()
    trace = json.loads((tmp_path / "tool_runs" / "01-zap_client_spider.json").read_text())
    assert trace["links"] == 5 and trace["artifacts"] == {"client_map.yaml": str(copied)}
    assert not orch.store.get_links(kind="redirect")
