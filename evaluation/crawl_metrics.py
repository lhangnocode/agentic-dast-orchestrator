"""
Run-level crawl metrics and their aggregation over repeated runs (max-recall protocol).

Definitions (TP = ground-truth entries matched by at least one discovered endpoint):
  matching               by canonical hash (default), or by path only when the ground-truth file
                         sets "match": "path" (method and query ignored; Crawlground scores a marker
                         on any GET or POST to its path, whatever the query string)
  recall                 TP / |GT|                       (all ground-truth entries)
  recall_reviewed        TP_r / |GT_r|                   GT_r = entries whose `reachable` is True
                                                         (only when a manual review exists)
  recall_<kind>          per ground-truth kind, e.g. client_route vs server_endpoint
  precision              discovered endpoints matching a GT entry / |discovered|
                                                         (out-of-GT finds are not necessarily
                                                          wrong: GT built from source can be incomplete)
  unique_valid           discovered endpoints with status 2xx/3xx/401/403
  duplicate_rate         1 - |discovered| / raw results  (how much tool output was redundant)
  time_to_saturation_s   run_started -> run_finished
  t50_s / t90_s          seconds until 50% / 90% of the run's final TP were discovered
                         (a GT entry counts as discovered with its first matching endpoint)
  requests, failed_runs, timeout_runs   from execution_history
  startup_overhead_s     Σ over tool runs of (first request at the target − tool invoked)
  crawl_time_s           Σ over the same tool runs of (tool run duration − its startup)
  startup_share          startup_overhead_s / (startup_overhead_s + crawl_time_s)
                         (tool runs that sent no request have no startup and are counted in
                          startup_unmeasured_runs; if a run that sent requests lacks a value, the totals
                          are None and startup_incomplete counts those runs; runs recorded before the
                          measurement existed give None)

Aggregation reports mean, sample standard deviation and a 95% t-interval over the runs.
"""

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Two-sided 95% critical values of Student's t (df = n - 1); 1.96 beyond 30
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
       10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
       18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
       26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}

VALID_STATUSES = range(200, 400)


MATCH_MODES = ("hash", "path")


def load_ground_truth(path: str) -> List[Dict[str, Any]]:
    """Entries of a ground-truth file; a file-level "match" mode is copied onto every entry."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return data
    mode = data.get("match", "hash")
    if mode not in MATCH_MODES:
        raise ValueError(f"Unknown ground-truth match mode {mode!r} in {path}")
    return [{**e, "match": e.get("match", mode)} for e in data["entries"]]


def _path_of(entry: Dict[str, Any]) -> str:
    return (entry.get("path_template") or "").split("?", 1)[0]


def _matches(gt_entry: Dict[str, Any], by_hash: Dict[str, List[Dict[str, Any]]],
             by_path: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Discovered endpoints that count as finding this ground-truth entry."""
    if gt_entry.get("match", "hash") == "path":
        return by_path.get(_path_of(gt_entry), [])
    return by_hash.get(gt_entry["canonical_hash"], [])


def _ts(value: str) -> datetime:
    """Parses run-log ISO timestamps and SQLite CURRENT_TIMESTAMP values (both UTC)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_run(run_dir: str) -> Dict[str, Any]:
    """Reads one run's store and JSONL events from its directory."""
    run = Path(run_dir)
    conn = sqlite3.connect(run / "state.db")
    conn.row_factory = sqlite3.Row
    endpoints = [dict(r) for r in conn.execute("SELECT * FROM endpoints")]
    history = [dict(r) for r in conn.execute("SELECT * FROM execution_history ORDER BY id")]
    conn.close()
    events_file = run / "run_events.jsonl"
    events = [json.loads(line) for line in events_file.read_text().splitlines()] if events_file.exists() else []
    return {"endpoints": endpoints, "history": history, "events": events}


def _time_to_fraction(times: List[float], fraction: float) -> Optional[float]:
    if not times:
        return None
    needed = max(1, math.ceil(fraction * len(times)))
    return round(sorted(times)[needed - 1], 1)


def run_metrics(endpoints: List[Dict[str, Any]], gt: List[Dict[str, Any]],
                history: Iterable[Dict[str, Any]] = (), events: Iterable[Dict[str, Any]] = ()) -> Dict[str, Any]:
    history, events = list(history), list(events)
    discovered = {e["canonical_hash"]: e for e in endpoints}
    gt_by_hash = {g["canonical_hash"]: g for g in gt}
    by_hash: Dict[str, List[Dict[str, Any]]] = {h: [e] for h, e in discovered.items()}
    by_path: Dict[str, List[Dict[str, Any]]] = {}
    for e in discovered.values():
        by_path.setdefault(_path_of(e), []).append(e)
    found = {h: _matches(g, by_hash, by_path) for h, g in gt_by_hash.items()}
    found = {h: eps for h, eps in found.items() if eps}
    tp = set(found)
    relevant = {e["canonical_hash"] for eps in found.values() for e in eps}

    m: Dict[str, Any] = {
        "discovered": len(discovered),
        "ground_truth": len(gt_by_hash),
        "true_positives": len(tp),
        "recall": round(len(tp) / len(gt_by_hash), 4) if gt_by_hash else None,
        "precision": round(len(relevant) / len(discovered), 4) if discovered else None,
        "unique_valid": sum(1 for e in endpoints if (e.get("status_code") or 0) in VALID_STATUSES
                            or e.get("status_code") in (401, 403)),
        "spa_routes_discovered": sum(1 for e in endpoints if "#" in (e.get("path_template") or "")),
    }
    for kind in sorted({g.get("kind", "endpoint") for g in gt}):
        of_kind = {h for h, g in gt_by_hash.items() if g.get("kind", "endpoint") == kind}
        m[f"recall_{kind}"] = round(len(of_kind & tp) / len(of_kind), 4) if of_kind else None
    reviewed = {h for h, g in gt_by_hash.items() if g.get("reachable") is True}
    m["recall_reviewed"] = round(len(reviewed & tp) / len(reviewed), 4) if reviewed else None

    raw = sum(h.get("raw_results") or 0 for h in history)
    m["raw_results"] = raw
    m["duplicate_rate"] = round(1 - len(discovered) / raw, 4) if raw else None
    m["requests"] = sum(h.get("requests_sent") or 0 for h in history)
    m["tool_runs"] = len(history)
    m["failed_runs"] = sum(1 for h in history if h.get("status") == "failed")
    m["timeout_runs"] = sum(1 for h in history if h.get("status") == "timeout")
    m.update(startup_metrics(history))

    started = next((e["ts"] for e in events if e.get("event") == "run_started"), None)
    finished = next((e for e in events if e.get("event") == "run_finished"), None)
    if started and finished:
        m["time_to_saturation_s"] = round((_ts(finished["ts"]) - _ts(started)).total_seconds(), 1)
        m["termination"] = finished.get("termination")
    if started:
        t0 = _ts(started)
        tp_times = []
        for eps in found.values():
            stamps = [_ts(e["created_at"]) for e in eps if e.get("created_at")]
            if stamps:
                tp_times.append(max(0.0, (min(stamps) - t0).total_seconds()))
        m["t50_s"] = _time_to_fraction(tp_times, 0.5)
        m["t90_s"] = _time_to_fraction(tp_times, 0.9)
        m["discovery_curve"] = [[round(t, 1), i + 1] for i, t in enumerate(sorted(tp_times))]
    return m


def startup_metrics(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Tool startup overhead vs crawl time, from execution_history.startup_seconds."""
    if not history or "startup_seconds" not in history[0]:
        return {"startup_overhead_s": None, "crawl_time_s": None, "startup_share": None,
                "startup_unmeasured_runs": None, "startup_by_tool": None}
    measured = [h for h in history if h.get("startup_seconds") is not None]
    # A tool run that sent requests (Firecrawl reports none, so always) must have a startup value;
    # if one is missing the run's totals would be partial, so they are not reported.
    missing = [h for h in history if h.get("startup_seconds") is None
               and ((h.get("requests_sent") or 0) > 0 or h.get("chosen_tool") == "firecrawl_navigate")]
    startup = sum(h["startup_seconds"] for h in measured)
    crawl = sum(max(0.0, (h.get("tool_duration_seconds") or 0) - h["startup_seconds"]) for h in measured)
    by_tool: Dict[str, Dict[str, Any]] = {}
    for h in history:
        t = by_tool.setdefault(h["chosen_tool"], {"runs": 0, "startup_s": [], "crawl_s": []})
        t["runs"] += 1
        if h.get("startup_seconds") is not None:
            t["startup_s"].append(round(h["startup_seconds"], 1))
            t["crawl_s"].append(round(max(0.0, (h.get("tool_duration_seconds") or 0) - h["startup_seconds"]), 1))
    ok = bool(measured) and not missing
    return {"startup_overhead_s": round(startup, 1) if ok else None,
            "crawl_time_s": round(crawl, 1) if ok else None,
            "startup_share": round(startup / (startup + crawl), 4) if ok and startup + crawl else None,
            "startup_unmeasured_runs": len(history) - len(measured),
            "startup_incomplete": len(missing),
            "startup_by_tool": by_tool}


def aggregate(runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Mean, sample SD and 95% t-interval for every numeric metric present in all runs."""
    out: Dict[str, Dict[str, Any]] = {}
    keys = sorted({k for r in runs for k, v in r.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
    for key in keys:
        values = [r[key] for r in runs if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
        n = len(values)
        if not n:
            continue
        mean = sum(values) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
        half = (T95.get(n - 1, 1.96) * sd / math.sqrt(n)) if n > 1 else 0.0
        out[key] = {"n": n, "mean": round(mean, 4), "sd": round(sd, 4),
                    "ci95": [round(mean - half, 4), round(mean + half, 4)]}
    return out
