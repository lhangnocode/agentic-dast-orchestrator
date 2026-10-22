import json
import sqlite3
from pathlib import Path

from normalization.normalizer import normalize_endpoint
from orchestrator.run_log import RunLog
from storage.state_store import StateStore

BASE = "http://localhost:3000"


def _ep(path, tool, status=200, content_type=""):
    ep = normalize_endpoint(f"{BASE}{path}", source_tool=tool, status_code=status)
    ep["content_type"] = content_type
    return ep


def _history(store, tool, **extra):
    store.record_iteration({"iteration": 1, "chosen_tool": tool, "endpoints_before": 0, "endpoints_after": 0,
                            "new_endpoints_discovered": 0, "marginal_gain": 0.0, **extra})


def test_migrates_a_pre_observation_database(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE endpoints (canonical_hash TEXT PRIMARY KEY, canonical_key TEXT UNIQUE NOT NULL,
            method TEXT NOT NULL, path_template TEXT NOT NULL, query_params TEXT NOT NULL,
            normalized_url TEXT NOT NULL, status_code INTEGER DEFAULT 200,
            first_seen_iteration INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE execution_history (id INTEGER PRIMARY KEY AUTOINCREMENT, iteration INTEGER NOT NULL,
            chosen_tool TEXT NOT NULL, reasoning TEXT, target_seeds TEXT, endpoints_before INTEGER NOT NULL,
            endpoints_after INTEGER NOT NULL, new_endpoints_discovered INTEGER NOT NULL, marginal_gain REAL NOT NULL,
            tool_duration_seconds REAL DEFAULT 0.0, llm_prompt_tokens INTEGER DEFAULT 0,
            llm_completion_tokens INTEGER DEFAULT 0, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO execution_history (iteration, chosen_tool, endpoints_before, endpoints_after,
            new_endpoints_discovered, marginal_gain) VALUES (1, 'zap_traditional_spider', 0, 5, 5, 5.0);
    """)
    conn.commit(); conn.close()

    store = StateStore(db_path=str(db))
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(execution_history)")}
    assert {"status", "error", "rule_id", "frontier_fingerprint", "seeds_count", "raw_results", "requests_sent"} <= cols
    assert "content_type" in {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(endpoints)")}
    # Old rows survive and get defaults; new rows can be written
    assert store.get_last_runs()["zap_traditional_spider"]["status"] == "ok"
    _history(store, "wapiti_form_crawl", status="timeout", error="max_scan_time", rule_id="R1")
    assert store.get_last_runs()["wapiti_form_crawl"]["status"] == "timeout"


def test_content_type_round_trip_and_backfill(tmp_path):
    store = StateStore(db_path=str(tmp_path / "s.db"))
    store.insert_or_update_endpoint(_ep("/api/Products/1", "firecrawl_navigate", status=0), 1)
    assert store.get_all_endpoints()[0]["content_type"] == ""
    # A later observation with a real content type fills the gap (parameters after ';' dropped)
    store.insert_or_update_endpoint(_ep("/api/Products/2", "zap_traditional_spider", 200,
                                        "application/json; charset=utf-8"), 2)
    row = store.get_all_endpoints()[0]
    assert row["content_type"] == "application/json" and row["status_code"] == 200


def test_frontier_fingerprint_tracks_changes(tmp_path):
    store = StateStore(db_path=str(tmp_path / "s.db"))
    store.insert_or_update_endpoint(_ep("/docs/a", "wapiti_form_crawl"), 1)
    fp1 = store.frontier_fingerprint("zap_traditional_spider")
    assert fp1.startswith("1:")
    assert store.frontier_fingerprint("zap_traditional_spider") == fp1          # stable
    store.insert_or_update_endpoint(_ep("/docs/b", "wapiti_form_crawl"), 2)
    assert store.frontier_fingerprint("zap_traditional_spider") != fp1          # grew
    assert store.frontier_fingerprint("wapiti_form_crawl") == "0:" + __import__("hashlib").sha1(b"").hexdigest()[:12]


def test_run_log_writes_jsonl(tmp_path):
    log = RunLog(str(tmp_path / "logs" / "events.jsonl"), run_id="r1")
    log.event("tool_run", tool="zap_traditional_spider", new_endpoints_discovered=3)
    log.event("run_finished", total=3)
    lines = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert [l["event"] for l in lines] == ["tool_run", "run_finished"]
    assert all(l["run_id"] == "r1" and "ts" in l for l in lines)
    RunLog(None).event("ignored")  # disabled log is a no-op
