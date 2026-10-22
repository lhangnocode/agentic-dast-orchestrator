from pathlib import Path

import pytest

from normalization.normalizer import NormalizationProfile, normalize_endpoint
from tools.client_map import parse_client_map

FIXTURE = Path(__file__).parent / "fixtures" / "client_map_juice_shop.yaml"
HOST = "http://host.docker.internal:3000"


def test_builds_urls_from_tree():
    export = '''- node: "ClientMap"
  children:
  - node: "http://app:8080"
    children:
    - node: "/#"
      children:
      - node: "/login"
        visited: true
    - node: "ftp"
      children:
      - node: "legal.md"
    - node: "redirect(to)"
'''.splitlines()
    assert parse_client_map(export) == [
        "http://app:8080",
        "http://app:8080/#",
        "http://app:8080/#/login",
        "http://app:8080/ftp",
        "http://app:8080/ftp/legal.md",
        "http://app:8080/redirect?to=",
    ]


def test_params_only_node_keeps_children_on_parent_page():
    export = '''- node: "http://app"
  children:
  - node: "(email,password)"
    children:
    - node: "/#"
      children:
      - node: "/about"
'''.splitlines()
    urls = parse_client_map(export)
    assert "http://app?email=&password=" in urls
    assert "http://app/#/about" in urls
    assert not any("(" in u for u in urls)


def test_survives_malformed_export_from_zap():
    # The fixture is a real ZAP 2.17.0 export that a YAML loader rejects
    yaml = pytest.importorskip("yaml")
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(FIXTURE.read_text())

    urls = parse_client_map(FIXTURE.open())
    routes = {u[len(HOST):] for u in urls if u.startswith(HOST + "/#/")}
    assert {"/#/login", "/#/search", "/#/deluxe-membership", "/#/complain"} <= routes
    assert f"{HOST}/rest/products/search?q=" in urls
    assert len(urls) == len(set(urls))


def test_hrefs_can_be_excluded():
    export = ['- node: "http://app"', '  components:', '  - href: "http://app/#/about"']
    assert "http://app/#/about" in parse_client_map(export)
    assert parse_client_map(export, include_hrefs=False) == ["http://app"]


def test_routes_normalize_to_spa_endpoints():
    profile = NormalizationProfile.from_config({
        "target": {"base_url": "http://localhost:3000/"},
        "normalization": {"host_aliases": ["localhost", "host.docker.internal"]},
    })
    keys = {ep["canonical_key"] for u in parse_client_map(FIXTURE.open())
            if (ep := normalize_endpoint(u, profile=profile))}
    assert {"GET #/login", "GET #/deluxe-membership", "GET /rest/products/search?q={q}"} <= keys


class _Recorder:
    """Minimal stand-in for the zapv2 client: records calls, serves canned data."""

    def __init__(self, host_dir, export_text):
        self.calls = []
        outer = self

        class Core:
            version = "2.17.0"

            def number_of_messages(self, baseurl=None):
                return 0

            def messages(self, baseurl, start, count):
                if start:
                    return []
                return [{"requestHeader": f"GET {HOST}/rest/languages HTTP/1.1",
                         "responseHeader": "HTTP/1.1 200 OK\r\nContent-Type: application/json"}]

        class ClientSpider:
            def __getattr__(self, name):           # set_option_* setters
                return lambda *a, **k: outer.calls.append((name, a))

            def scan(self, **kwargs):
                outer.calls.append(("scan", kwargs))
                return "0"

            def status(self, scan_id):
                return "100"

        class Client:
            def export_client_map(self, path):
                outer.calls.append(("export", path))
                (host_dir / "client_map.yaml").write_text(export_text)
                return "OK"

        class Replacer:
            rules = []

            def add_rule(self, **kwargs):
                outer.calls.append(("add_rule", kwargs["description"]))

        self.core, self.clientSpider, self.client, self.replacer = Core(), ClientSpider(), Client(), Replacer()


def _wrapper(tmp_path, export_text):
    from tools.zap_wrapper import ZapWrapper
    zap = ZapWrapper(target_base="http://localhost:3000/", target_host_alias="host.docker.internal",
                     client_options={"poll_interval_sec": 0},
                     client_map_export={"host_dir": str(tmp_path), "container_dir": "/zap/wrk"})
    fake = _Recorder(tmp_path, export_text)
    zap._client = fake
    return zap, fake


def test_run_client_spider_merges_messages_and_client_map(tmp_path):
    zap, fake = _wrapper(tmp_path, FIXTURE.read_text())
    zap.set_auth({"Authorization": "Bearer t"}, {})
    results = zap.run_client_spider(["http://localhost:3000/", "http://localhost:3000/#/search"])

    scans = [kw for name, kw in fake.calls if name == "scan"]
    assert [s["url"] for s in scans] == ["http://host.docker.internal:3000/", "http://host.docker.internal:3000/#/search"]
    assert "contextname" not in scans[0]                       # no browser login configured
    assert ("add_rule", "dast-orchestrator-auth:Authorization") in fake.calls  # header fallback active
    assert ("export", "/zap/wrk/client_map.yaml") in fake.calls

    urls = {r["url"] for r in results}
    assert "http://localhost:3000/rest/languages" in urls       # HTTP message, mapped back to localhost
    assert "http://localhost:3000/#/deluxe-membership" in urls  # route only the Client Map knows
    assert all(r["source"] == "zap_client_spider" for r in results)
    assert zap.last_request_count == 1 and zap.last_status == "ok"


def test_run_client_spider_as_logged_in_user(tmp_path):
    zap, fake = _wrapper(tmp_path, '- node: "http://host.docker.internal:3000"\n')
    zap.client_spider_user = ("dast-orchestrator", "admin")
    zap.run_client_spider(["http://localhost:3000/"])
    scan = next(kw for name, kw in fake.calls if name == "scan")
    assert scan["contextname"] == "dast-orchestrator" and scan["username"] == "admin"


def test_missing_export_volume_is_reported_not_fatal(tmp_path):
    zap, fake = _wrapper(tmp_path, "")
    zap.client_map_export["host_dir"] = str(tmp_path / "not-mounted")
    results = zap.run_client_spider(["http://localhost:3000/"])
    assert [r["url"] for r in results] == ["http://localhost:3000/rest/languages"]


def test_per_seed_duration_applies_only_to_multi_seed_runs(tmp_path):
    zap, fake = _wrapper(tmp_path, "")
    zap.client_options.update(max_duration_min=0, per_seed_max_duration_min=2)
    zap.run_client_spider(["http://localhost:3000/"])
    assert ("set_option_max_duration", (2,)) not in fake.calls          # single base scan: unlimited
    zap.run_client_spider(["http://localhost:3000/a", "http://localhost:3000/b"])
    assert ("set_option_max_duration", (2,)) in fake.calls
