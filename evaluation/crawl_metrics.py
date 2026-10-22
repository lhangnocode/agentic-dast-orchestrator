"""
Run-level crawl metrics and their aggregation over repeated runs (max-recall protocol).

Definitions (TP = discovered canonical hashes that are in the ground truth):
  recall                 TP / |GT|                       (all ground-truth entries)
  recall_reviewed        TP_r / |GT_r|                   GT_r = entries whose `reachable` is True
                                                         (only when a manual review exists)
  recall_<kind>          per ground-truth kind, e.g. client_route vs server_endpoint
  precision              TP / |discovered|               (out-of-GT finds are not necessarily
                                                          wrong: GT built from source can be incomplete)
  unique_valid           discovered endpoints with status 2xx/3xx/401/403
  duplicate_rate         1 - |discovered| / raw results  (how much tool output was redundant)
  time_to_saturation_s   run_started -> run_finished
  t50_s / t90_s          seconds until 50% / 90% of the run's final TP were discovered
  requests, failed_runs, timeout_runs   from execution_history

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


def load_ground_truth(path: str) -> List[Dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["entries"] if isinstance(data, dict) else data


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
    tp = set(discovered) & set(gt_by_hash)

    m: Dict[str, Any] = {
        "discovered": len(discovered),
        "ground_truth": len(gt_by_hash),
        "true_positives": len(tp),
        "recall": round(len(tp) / len(gt_by_hash), 4) if gt_by_hash else None,
        "precision": round(len(tp) / len(discovered), 4) if discovered else None,
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

    started = next((e["ts"] for e in events if e.get("event") == "run_started"), None)
    finished = next((e for e in events if e.get("event") == "run_finished"), None)
    if started and finished:
        m["time_to_saturation_s"] = round((_ts(finished["ts"]) - _ts(started)).total_seconds(), 1)
        m["termination"] = finished.get("termination")
    if started:
        t0 = _ts(started)
        tp_times = [max(0.0, (_ts(discovered[h]["created_at"]) - t0).total_seconds())
                    for h in tp if discovered[h].get("created_at")]
        m["t50_s"] = _time_to_fraction(tp_times, 0.5)
        m["t90_s"] = _time_to_fraction(tp_times, 0.9)
        m["discovery_curve"] = [[round(t, 1), i + 1] for i, t in enumerate(sorted(tp_times))]
    return m


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
