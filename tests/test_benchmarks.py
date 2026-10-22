from pathlib import Path

from evaluation.crawl_metrics import aggregate, run_metrics
from evaluation.ground_truth import juice_shop_routes, juice_shop_server_endpoints
from evaluation.run_benchmark import apply_override, deep_merge, run_config

ROUTING_TS = """
const routes: Routes = [
  { path: 'administration', component: A, canActivate: [AdminGuard] },
  { path: 'privacy-security', component: P,
    children: [
      { path: 'privacy-policy', component: PP },
      { path: 'change-password', component: CP }
    ]
  },
  { path: 'chatbot', component: C, children: [ { path: '', component: W }, { path: 'conversation/:id', component: V } ] },
  // { path: 'disabled', component: X },
  { path: 'search', component: S },
  { path: '**', component: S }
]
"""

SERVER_TS = """
  app.get('/rest/languages', languageList())
  // app.put('/api/Products/:id', security.isAuthorized())
  app.post('/rest/user/login', login())
  app.use('/rest/basket', security.isAuthorized())
  const autoModels = [
    { name: 'User', exclude: ['password'], model: UserModel },
    { name: 'Product', exclude: [], model: ProductModel }
  ]
"""


def test_juice_shop_routes_follow_nesting_and_skip_wildcards():
    assert juice_shop_routes(ROUTING_TS) == [
        "administration", "privacy-security", "privacy-security/privacy-policy",
        "privacy-security/change-password", "chatbot", "chatbot/conversation/:id", "search",
    ]


def test_juice_shop_server_endpoints():
    eps = juice_shop_server_endpoints(SERVER_TS)
    assert ("GET", "/rest/languages") in eps and ("POST", "/rest/user/login") in eps
    assert ("PUT", "/api/Products/:id") in eps            # from autoModels, not the commented line
    assert ("GET", "/api/Users") in eps and ("DELETE", "/api/Users/:id") in eps
    assert not any(p == "/rest/basket" for _, p in eps)   # app.use mounts middleware, not an endpoint


def _gt(key, kind, reachable=None):
    return {"canonical_hash": key, "canonical_key": key, "kind": kind, "reachable": reachable}


def test_run_metrics():
    gt = [_gt("GET #/login", "client_route", True), _gt("GET #/basket", "client_route"),
          _gt("GET /api/Users", "server_endpoint", True), _gt("POST /rest/user/login", "server_endpoint")]
    endpoints = [
        {"canonical_hash": "GET #/login", "path_template": "#/login", "status_code": 0, "created_at": "2026-10-12 10:00:10"},
        {"canonical_hash": "GET /api/Users", "path_template": "/api/Users", "status_code": 200, "created_at": "2026-10-12 10:01:00"},
        {"canonical_hash": "GET /extra", "path_template": "/extra", "status_code": 404, "created_at": "2026-10-12 10:02:00"},
    ]
    history = [{"raw_results": 10, "requests_sent": 30, "status": "ok"},
               {"raw_results": 2, "requests_sent": 5, "status": "timeout"}]
    events = [{"event": "run_started", "ts": "2026-10-12T10:00:00.000+00:00"},
              {"event": "run_finished", "ts": "2026-10-12T10:05:00.000+00:00", "termination": "saturation"}]
    m = run_metrics(endpoints, gt, history, events)
    assert (m["true_positives"], m["recall"], m["precision"]) == (2, 0.5, 0.6667)
    assert m["recall_client_route"] == 0.5 and m["recall_server_endpoint"] == 0.5
    assert m["recall_reviewed"] == 1.0                    # both reviewed-reachable entries were found
    assert m["duplicate_rate"] == 0.75 and m["requests"] == 35 and m["timeout_runs"] == 1
    assert m["time_to_saturation_s"] == 300.0 and m["termination"] == "saturation"
    assert m["t50_s"] == 10.0 and m["t90_s"] == 60.0
    assert m["discovery_curve"] == [[10.0, 1], [60.0, 2]]
    assert m["unique_valid"] == 1 and m["spa_routes_discovered"] == 1


def test_aggregate_mean_sd_ci():
    agg = aggregate([{"recall": 0.5}, {"recall": 0.6}, {"recall": 0.7}])["recall"]
    assert agg["n"] == 3 and agg["mean"] == 0.6 and agg["sd"] == 0.1
    assert agg["ci95"] == [round(0.6 - 4.303 * 0.1 / 3 ** 0.5, 4), round(0.6 + 4.303 * 0.1 / 3 ** 0.5, 4)]


def test_run_config_isolates_every_output(tmp_path):
    base = {"target": {"base_url": "http://x/"}, "tools": {"wapiti": {"binary": "wapiti"}}}
    profile = {"config": {"target": {"base_url": "http://localhost:3456/"}, "auth": {"enabled": False}}}
    cfg = run_config(base, profile, ["orchestrator.rules.browser_spider=zap_ajax_spider",
                                     "orchestrator.max_iterations=5"], tmp_path)
    assert cfg["target"]["base_url"] == "http://localhost:3456/"
    assert cfg["orchestrator"] == {"rules": {"browser_spider": "zap_ajax_spider"}, "max_iterations": 5}
    for value in (cfg["storage"]["sqlite_db"], cfg["logging"]["run_events_file"], cfg["output"]["dir"],
                  cfg["tools"]["wapiti"]["session_file"], cfg["tools"]["firecrawl"]["prompts_dir"]):
        assert value.startswith(str(tmp_path))
    assert cfg["tools"]["wapiti"]["binary"] == "wapiti"   # untouched keys survive the merge
    assert deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}


def test_shipped_ground_truth_files_are_consistent():
    import json
    root = Path(__file__).resolve().parents[1] / "evaluation" / "ground_truth"
    for name, kinds in [("juice-shop", {"client_route", "server_endpoint"}), ("crawlground", {"marker"}),
                        ("crawl-maze", {"expected_resource"})]:
        doc = json.loads((root / f"{name}.json").read_text())
        hashes = [e["canonical_hash"] for e in doc["entries"]]
        assert len(hashes) == len(set(hashes)) and {e["kind"] for e in doc["entries"]} == kinds


def test_restart_zap_restarts_container_and_waits(monkeypatch):
    import evaluation.run_benchmark as rb
    calls = []
    monkeypatch.setattr(rb.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(rb.time, "sleep", lambda s: None)

    class Zap:
        _client = "old"
        answers = iter([False, False, True])

        def is_alive(self):
            return next(self.answers)

    zap = Zap()
    rb.restart_zap(zap)
    assert calls == [["docker", "compose", "-f", str(rb.ZAP_COMPOSE_FILE), "restart", "zap"]]
    assert zap._client is None                      # reconnects to the new daemon
