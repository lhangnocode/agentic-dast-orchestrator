"""
Consolidates the Security Crawl Maze series (ZAP only, Wapiti only, ZAP + Wapiti, Firecrawl only, full
pipeline; 2 runs each) into output/benchmarks/crawl-maze/series-summary.md, and writes one detailed
iteration report per full-pipeline run (output/benchmarks/crawl-maze/full-pipeline/run-NN/iterations.md).

Every number is computed here from the run folders: metrics.json, state.db, run_events.jsonl,
tool_runs/*.json (one trace per tool run), config.json, run_meta.json, target_access.log (the Crawl Maze
server's own access log) and tunnel_traffic.jsonl (requests that came through the public tunnel, i.e.
Firecrawl's). Stored metrics are recomputed from the raw data and cross-checked; the result of every check
is listed at the end of the summary and the script exits with status 1 if any check fails.

  PYTHONPATH=. .venv/bin/python3 output/consolidate_maze.py
"""
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evaluation.crawl_metrics import _path_of, _ts, load_ground_truth, load_run, run_metrics
from evaluation.run_benchmark import access_log_metrics, traffic_metrics
from tools.traffic_proxy import read_traffic

ROOT = Path("output/benchmarks/crawl-maze")
GT = load_ground_truth("evaluation/ground_truth/crawl-maze.json")   # match mode "path"
GT_PATHS = {_path_of(g) for g in GT}


def gt_hits(keys):
    return sorted({k.split(" ", 1)[1].split("?", 1)[0] for k in keys} & GT_PATHS)


def first_finds(traces):
    """Expected resources each tool run found FIRST (in execution order). A resource found again later under
    another method or query (a new endpoint on the same path) is not counted twice."""
    seen, out = set(), {}
    for t in traces:
        hits = [p for p in gt_hits(t["new_endpoints"]) if p not in seen]
        seen.update(hits)
        out[id(t)] = hits
    return out

CONFIGS = [("ZAP only", "zap-only"), ("Wapiti only", "wapiti-only"), ("ZAP + Wapiti", "zap-wapiti"),
           ("Firecrawl only", "firecrawl-only"), ("Full pipeline", "full-pipeline")]
EXPECTED_RUNS = 2
TOOL = {"zap_traditional_spider": "ZAP traditional spider", "zap_client_spider": "ZAP Client Spider",
        "wapiti_form_crawl": "Wapiti", "firecrawl_navigate": "Firecrawl", "http_probe": "HTTP probe"}
SHORT = {"zap_traditional_spider": "ZAP trad.", "zap_client_spider": "ZAP Client", "wapiti_form_crawl": "Wapiti",
         "firecrawl_navigate": "Firecrawl", "http_probe": "Probe"}
CHECKS = []


def check(desc, ok, detail=""):
    CHECKS.append((desc, bool(ok), detail))


def fmt(v, kind="int"):
    if v is None:
        return "–"
    if kind == "ratio":
        return f"{v:.4f}"
    if kind == "min":
        return f"{v / 60:.1f}"
    if kind == "s":
        return f"{v:.1f}"
    if kind == "pct":
        return f"{100 * v:.1f}%"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%S") if epoch else "–"


# ------------------------------------------------------------------ load + verify
def load(label):
    runs = []
    for d in sorted((ROOT / label).glob("run-[0-9][0-9]")):
        if not (d / "metrics.json").exists():
            ABORTED.append(aborted_info(label, d))
            continue
        stored = json.loads((d / "metrics.json").read_text())
        raw = load_run(str(d))
        fresh = run_metrics(gt=GT, **raw)
        fresh.update(traffic_metrics(d))
        fresh.update(access_log_metrics(d, GT))
        for k in ("recall", "precision", "true_positives", "discovered", "requests", "time_to_saturation_s",
                  "t90_s", "startup_overhead_s", "server_resources_requested", "firecrawl_requests_observed"):
            check(f"{label}/{d.name}: {k} recomputed from raw data", stored.get(k) == fresh.get(k),
                  f"stored {stored.get(k)} vs raw {fresh.get(k)}")
        order = {(h["iteration"], h["chosen_tool"]): i for i, h in enumerate(raw["history"])}   # ingestion order
        traces = sorted((json.loads(f.read_text()) for f in (d / "tool_runs").glob("*.json")),
                        key=lambda t: order.get((t["iteration"], t["tool"]), 1e9))
        check(f"{label}/{d.name}: one trace file per history row", len(traces) == len(raw["history"]),
              f"{len(traces)} traces, {len(raw['history'])} rows")
        n_first = sum(len(v) for v in first_finds(traces).values())
        check(f"{label}/{d.name}: first finds per tool run add up to the expected resources found",
              n_first == stored.get("true_positives"), f"{n_first} vs {stored.get('true_positives')}")
        meta =json.loads((d / "run_meta.json").read_text())
        found = {_path_of(e) for e in raw["endpoints"]} & GT_PATHS
        log_paths = {l for l in access_paths(d)} & GT_PATHS
        runs.append({"dir": d, "name": d.name, "m": stored, "raw": raw, "traces": traces, "meta": meta,
                     "config": json.loads((d / "config.json").read_text()), "found": found,
                     "requested": log_paths})
    return runs


ABORTED = []


def aborted_info(label, d):
    """What an unfinished run did before it stopped (kept on disk, excluded from every statistic)."""
    events = [json.loads(l) for l in (d / "run_events.jsonl").read_text().splitlines()] \
        if (d / "run_events.jsonl").exists() else []
    start = next((e["ts"] for e in events if e.get("event") == "run_started"), None)
    jobs = sorted((d / "firecrawl_responses").glob("*.json")) if (d / "firecrawl_responses").exists() else []
    traces = sorted((d / "tool_runs").glob("*.json")) if (d / "tool_runs").exists() else []
    return {"run": f"{label}/{d.name}", "started": start, "tool_runs_finished": [t.stem for t in traces],
            "firecrawl_jobs_saved": len(jobs)}


def access_paths(d):
    from evaluation.run_benchmark import ACCESS_LINE, ANSI
    log = d / "target_access.log"
    if not log.exists():
        return []
    return [m.group(2).split("?", 1)[0] for m in ACCESS_LINE.finditer(ANSI.sub("", log.read_text(encoding="utf-8")))]


DATA = {name: load(label) for name, label in CONFIGS}
for name, runs in DATA.items():
    check(f"{name}: {EXPECTED_RUNS} completed runs", len(runs) == EXPECTED_RUNS, f"{len(runs)} runs")

# ------------------------------------------------------------------ comparison table
ROWS = [
    ("Resources found (reported by tools) / 91", "true_positives", "int"),
    ("Resources the server saw requested / 91", "server_resources_requested", "int"),
    ("Recall", "recall", "ratio"),
    ("Discovered endpoints", "discovered", "int"),
    ("Precision", "precision", "ratio"),
    ("Unique valid endpoints (2xx/3xx/401/403)", "unique_valid", "int"),
    ("Duplicate rate", "duplicate_rate", "ratio"),
    ("Time to saturation (min)", "time_to_saturation_s", "min"),
    ("Time to 50% (t50, min)", "t50_s", "min"),
    ("Time to 90% (t90, min)", "t90_s", "min"),
    ("Requests reported by tools", "requests", "int"),
    ("Firecrawl requests seen on the tunnel", "firecrawl_requests_observed", "int"),
    ("Requests in the target's access log", "target_requests_logged", "int"),
    ("Tool runs", "tool_runs", "int"),
    ("Failed / timed-out tool runs", None, None),
    ("Tool startup overhead (s)", "startup_overhead_s", "s"),
    ("Crawl time, startup excluded (s)", "crawl_time_s", "s"),
    ("Startup share of tool time", "startup_share", "pct"),
    ("ZAP restart before the run (s)", "zap_restart_s", "s"),
]


def cell(runs, key, kind):
    if key is None:
        return " / ".join(f"{r['m'].get('failed_runs', 0)}/{r['m'].get('timeout_runs', 0)}" for r in runs)
    vals = [r["m"].get(key) for r in runs]
    if key == "firecrawl_requests_observed":
        vals = [v if r["config"]["tools"]["firecrawl"].get("enabled", True) is not False else None
                for v, r in zip(vals, runs)]
        if all(v is None for v in vals):
            return "n/a (Firecrawl off)"
    shown = " / ".join(fmt(v, kind) for v in vals)
    nums = [v for v in vals if isinstance(v, (int, float))]
    if len(nums) == len(vals) == 2 and nums[0] != nums[1]:
        shown += f" (mean {fmt(statistics.mean(nums), kind)})"
    return shown


def comparison_table():
    head = "| Metric | " + " | ".join(n for n, _ in CONFIGS) + " |"
    lines = [head, "|---|" + "---|" * len(CONFIGS)]
    for title, key, kind in ROWS:
        lines.append(f"| {title} | " + " | ".join(cell(DATA[n], key, kind) for n, _ in CONFIGS) + " |")
    return "\n".join(lines)


# ------------------------------------------------------------------ full pipeline iteration detail
PARAM_KEYS = {
    "zap_traditional_spider": ("zap", "spider", ["max_depth", "max_duration_min", "thread_count", "parse_sitemap_xml",
                                                 "parse_robots_txt", "process_form", "post_form"]),
    "zap_client_spider": ("zap", "client_spider", ["browser_id", "number_of_browsers", "max_depth", "max_children",
                                                   "max_duration_min", "per_seed_max_duration_min", "scope_check",
                                                   "logout_avoidance"]),
    "wapiti_form_crawl": ("wapiti", None, ["scope", "depth", "max_links_per_page", "max_parameters", "headless",
                                           "wait_sec", "max_scan_time", "timeout"]),
    "firecrawl_navigate": ("firecrawl", None, ["model", "batch_size", "max_seeds", "max_credits", "effort",
                                               "poll_timeout_seconds", "known_routes_max_chars"]),
    "http_probe": ("http_probe", None, ["timeout_sec", "max_workers"]),
}


def params(config, tool):
    section, sub, keys = PARAM_KEYS[tool]
    c = config["tools"][section]
    c = c.get(sub, {}) if sub else c
    return ", ".join(f"{k}={json.dumps(c.get(k))}" for k in keys if k in c)


def decisions(run):
    out = {}
    for e in run["raw"]["events"]:
        if e.get("event") == "decision":
            out.setdefault(e["iteration"], []).append(e)
    return out


def iteration_walls(traces):
    walls = {}
    for t in traces:
        s, e = t["started_at"], t["started_at"] + t["duration_seconds"]
        a, b = walls.get(t["iteration"], (s, e))
        walls[t["iteration"]] = (min(a, s), max(b, e))
    return walls


def next_decision(run, it, last_it):
    dec = decisions(run).get(it + 1)
    if dec:
        return "; ".join(f"{d['rule_id']} → {SHORT[d['action']]} ({d['seeds_count']} seeds)" for d in dec)
    if it < last_it:
        return "next fixed stage"
    m = run["m"]
    return f"stop: {m.get('termination')} (no rule fired for any tool)"


def iteration_rows(run, compact):
    traces, last_it = run["traces"], max(t["iteration"] for t in run["traces"])
    rows = []
    prev_total = 1
    first_tool_of_iter = {}
    for t in traces:
        first_tool_of_iter.setdefault(t["iteration"], t["tool"])
    firsts = first_finds(traces)
    for t in traces:
        hits = firsts[id(t)]
        distinct = len({r["url"] for r in t["raw_results"]})
        crawl = (t["duration_seconds"] - t["startup_seconds"]) if t["startup_seconds"] is not None else None
        seeds = t["seeds"]
        seed_txt = f"{len(seeds)}: " + ", ".join(s.replace("http://localhost:8090", "") or "/" for s in seeds[:3]) \
            + (" …" if len(seeds) > 3 else "")
        queue = ", ".join(f"{SHORT[k]} {v}" for k, v in t["pending_frontier_after"].items() if k in SHORT)
        state = f"endpoints {t['endpoints_total_after'] - len(t['new_endpoints'])}→{t['endpoints_total_after']}; " \
                f"pending: {queue}; visited by {SHORT[t['tool']]}: {t['processed_after'].get(t['tool'], 0)}"
        decision = next_decision(run, t["iteration"], last_it) \
            if t is [x for x in traces if x["iteration"] == t["iteration"]][-1] else "(see last tool of this iteration)"
        if compact:
            rows.append(f"| {t['iteration']} | {SHORT[t['tool']]} | {t['rule_id']}: {t['reason']} | {len(seeds)} | "
                        f"{len(t['raw_results'])} / {distinct} | {len(t['new_endpoints'])} | {len(hits)} | "
                        f"{fmt(t['duration_seconds'], 's')} | {fmt(t['startup_seconds'], 's')} | {fmt(crawl, 's')} | "
                        f"{t['requests_sent']} | {t['status']} | {decision} |")
        else:
            rows.append((t, seed_txt, state, decision, hits, distinct, crawl))
    return rows


COMPACT_HEAD = ("| Iter | Tool | Rule: reason | Input URLs | Output: raw / distinct URLs | New endpoints (added) | "
                "New GT resources | Tool time (s) | Startup (s) | Crawl (s) | Requests | Status | "
                "Orchestrator decision after this iteration |\n|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")


def run_summary(run):
    traces, m = run["traces"], run["m"]
    walls = iteration_walls(traces)
    wall_sum = sum(b - a for a, b in walls.values())
    freq = Counter(SHORT[t["tool"]] for t in traces)
    rules = Counter(t["rule_id"] for t in traces)
    tool_time = sum(t["duration_seconds"] for t in traces)
    by_tool = {}
    firsts = first_finds(traces)
    for t in traces:
        b = by_tool.setdefault(t["tool"], {"runs": 0, "time": 0.0, "startup": 0.0, "startup_n": 0, "new": 0,
                                           "gt": set(), "req": 0})
        b["runs"] += 1
        b["time"] += t["duration_seconds"]
        b["req"] += t["requests_sent"]
        b["new"] += len(t["new_endpoints"])
        b["gt"] |= set(firsts[id(t)])
        if t["startup_seconds"] is not None:
            b["startup"] += t["startup_seconds"]
            b["startup_n"] += 1
    total = m.get("time_to_saturation_s")
    lines = [
        f"- **Loops (iterations):** {max(walls)} ({len(traces)} tool runs; stages: "
        f"{sum(1 for t in traces if t['rule_id'].startswith('stage'))} tool runs, rule rounds: "
        f"{sum(1 for t in traces if not t['rule_id'].startswith('stage'))}); termination: {m.get('termination')}.",
        f"- **Tool selection frequency:** " + ", ".join(f"{k} {v}" for k, v in freq.items())
        + "; by rule: " + ", ".join(f"{k} {v}" for k, v in rules.items()) + ".",
        f"- **Total pipeline time (run start → stop):** {fmt(total, 's')} s ({fmt(total, 'min')} min). "
        f"Sum of iteration wall times {wall_sum:.1f} s; orchestration outside tool runs "
        f"(store, rules, normalization, between iterations) {total - wall_sum:.1f} s." if total else "",
        f"- **Tool time (sum over tool runs, parallel lanes counted separately):** {tool_time:.1f} s = "
        f"startup {fmt(m.get('startup_overhead_s'), 's')} s + crawl {fmt(m.get('crawl_time_s'), 's')} s "
        f"(startup share {fmt(m.get('startup_share'), 'pct')}); "
        f"{m.get('startup_unmeasured_runs', 0)} tool run(s) sent no request and have no startup value.",
        f"- **Discovery:** {m['discovered']} canonical endpoints, {m['true_positives']}/91 expected resources "
        f"reported (recall {m['recall']:.4f}), {m.get('server_resources_requested')}/91 requested at the server, "
        f"precision {m['precision']:.4f}, t50 {fmt(m.get('t50_s'), 'min')} min, t90 {fmt(m.get('t90_s'), 'min')} min.",
        f"- **Requests:** {m['requests']:,} reported by ZAP/Wapiti/probe + {m.get('firecrawl_requests_observed', 0):,} "
        f"Firecrawl requests seen on the tunnel; the target logged {m.get('target_requests_logged', 0):,}.",
        "",
        "| Tool | Runs | Tool time (s) | Startup (s) | New endpoints | Expected resources first found | Requests |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for tool, b in by_tool.items():
        startup = f"{b['startup']:.1f}" if b["startup_n"] else "–"
        if b["startup_n"] not in (0, b["runs"]):
            startup += f" ({b['startup_n']} of {b['runs']} runs)"
        lines.append(f"| {TOOL[tool]} | {b['runs']} | {b['time']:.1f} | {startup} | "
                     f"{b['new']} | {len(b['gt'])} | {b['req']:,} |")
    return "\n".join(l for l in lines if l is not None)


def write_iteration_report(run):
    d = run["dir"]
    out = [f"# Crawl Maze / full-pipeline / {run['name']}: iteration detail", "",
           "Generated by `output/consolidate_maze.py` from `tool_runs/*.json`, `run_events.jsonl`, `config.json` "
           "and `metrics.json` in this folder. Times are UTC. Startup = tool invoked → its first request at the "
           "target; crawl = tool time − startup.", "", "## Summary", "", run_summary(run), "",
           "## Iterations", ""]
    for t, seed_txt, state, decision, hits, distinct, crawl in iteration_rows(run, compact=False):
        out += [f"### Iteration {t['iteration']}: {TOOL[t['tool']]}", "",
                f"- **Why selected:** `{t['rule_id']}`: {t['reason']}" + (f" (focus: {t['focus']})" if t["focus"] else ""),
                f"- **Parameters:** {params(run['config'], t['tool'])}",
                f"- **Timing:** invoked {utc(t['started_at'])}, first request {utc(t['first_request_at'])}, "
                f"tool time {t['duration_seconds']:.1f} s = startup {fmt(t['startup_seconds'], 's')} s + crawl "
                f"{fmt(crawl, 's')} s; status {t['status']}" + (f", error: {t['error']}" if t["error"] else ""),
                f"- **Processed:** {len(t['seeds'])} input URL(s); {t['requests_sent']} request(s) reported by the tool",
                f"- **Discovered:** {len(t['raw_results'])} raw results ({distinct} distinct URLs) → "
                f"{len(t['new_endpoints'])} new canonical endpoint(s) added; {len(hits)} expected resource(s) first found",
                f"- **State after:** {state}",
                f"- **Orchestrator decision after this iteration:** {decision}", "",
                "<details><summary>Input URLs</summary>", "", *[f"- {s}" for s in t["seeds"]], "", "</details>", "",
                "<details><summary>New endpoints added by this run</summary>", "",
                *([f"- {k}" + (" **(expected resource)**" if k.split(' ', 1)[1].split('?', 1)[0] in GT_PATHS else "")
                   for k in t["new_endpoints"]] or ["- none"]), "", "</details>", "",
                "<details><summary>All raw output URLs</summary>", "",
                *([f"- {r['method']} {r['url']} ({r['status_code']})" for r in t["raw_results"]] or ["- none"]), "",
                "</details>", ""]
    (d / "iterations.md").write_text("\n".join(out), encoding="utf-8")


# ------------------------------------------------------------------ per-resource and setup sections
def resource_table():
    lines = ["| Expected resource | " + " | ".join(SHORT_C for SHORT_C in ("ZAP", "Wapiti", "ZAP+Wapiti", "Firecrawl", "Full"))
             + " |", "|---|" + "---:|" * 5]
    for p in sorted(GT_PATHS):
        cells = []
        for name, _ in CONFIGS:
            runs = DATA[name]
            cells.append(f"{sum(p in r['found'] for r in runs)}/{len(runs)}")
        lines.append(f"| `{p}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def addons():
    vals = {}
    for runs in DATA.values():
        for r in runs:
            for k, v in r["meta"].get("zap_addons", {}).items():
                vals.setdefault(k, set()).add(v)
    return ", ".join(f"{k} {'/'.join(sorted(v))}" for k, v in sorted(vals.items()) if k != "error")


def run_list():
    lines = ["| Config | Run | Started (UTC) | Recall | Found | Server saw | Discovered | Precision | Time (min) | "
             "Requests | Tunnel req. | Tool runs | Startup (s) | Termination |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name, label in CONFIGS:
        for r in DATA[name]:
            m = r["m"]
            start = next((e["ts"] for e in r["raw"]["events"] if e.get("event") == "run_started"), "")
            lines.append(f"| {name} | {label}/{r['name']} | {start[:19].replace('T', ' ')} | {m['recall']:.4f} | "
                         f"{m['true_positives']} | {m.get('server_resources_requested')} | {m['discovered']} | "
                         f"{m['precision']:.4f} | {fmt(m.get('time_to_saturation_s'), 'min')} | {m['requests']:,} | "
                         f"{m.get('firecrawl_requests_observed', '–')} | {m['tool_runs']} | "
                         f"{fmt(m.get('startup_overhead_s'), 's')} | {m.get('termination')} |")
    return "\n".join(lines)


NOTES = """- **Same setup for every run:** Crawl Maze container restarted, ZAP container restarted, the tunnel logging
  proxy on :3457 running (only Firecrawl reaches the tunnel), base URL `http://localhost:8090/` as the only
  seed, `configs/app_config.json` plus the overrides recorded in each run's `config.json` / `run_meta.json`.
- **Matching:** an expected resource counts as found when a discovered endpoint has its path (any method or
  query). *Found (reported)* comes from the tools' output; *server saw requested* comes from Crawl Maze's own
  access log (`target_access.log`), independent of the tools. They differ when a tool reports a URL it never
  requested (Firecrawl extracts links from page content) or requests one it does not report.
- **Precision** = discovered endpoints on an expected-resource path / discovered endpoints. The 91 expected
  resources are the test targets only; test pages, index pages and SPA routes count as "not in ground truth",
  so precision is low for every configuration by construction.
- **Unique valid endpoints** needs an HTTP status. Firecrawl returns links without status and the probe is off
  in *Firecrawl only*, so its 0 there means *not measurable*, not *no valid endpoints*.
- **Requests reported by tools** comes from ZAP/Wapiti/probe. Firecrawl's API does not report requests (its
  0 is *not reported*); its requests are counted on the tunnel instead (*Firecrawl requests seen on the
  tunnel*). *Requests in the target's access log* counts everything the target received, all tools included;
  in runs scored before the log capture used sub-second precision (ZAP only run 1, Wapiti only run 1,
  ZAP + Wapiti run 1) it can include the runner's one readiness check before the run.
- **Times:** *time to saturation* = run start → stop (whole pipeline, wall clock). *Tool startup overhead* =
  Σ over tool runs of (tool invoked → its first request at the target); *crawl time* = Σ (tool time −
  startup). Tool time is summed over tool runs, so lanes that ran in parallel are counted separately and
  the sum can exceed the wall-clock time. For a Firecrawl tool run with several jobs, startup is the delay to
  the first request of its first job; the other jobs' queueing delays are inside crawl time (per-job values
  are in `firecrawl_responses/<job>.json`: `startup_seconds`, `requests_observed`).
- **ZAP restart** is environment reset before the run and is in none of the other times.
- **n = 2:** every configuration has 2 complete runs. ZAP only, Wapiti only and ZAP + Wapiti gave identical
  recall in both runs; Firecrawl only and the full pipeline vary between runs (Firecrawl is an LLM agent).
  The full pipeline's run numbers are 02 and 04 because run-01 and run-03 were aborted (see "Aborted runs")."""

DEVIATIONS = """- **full-pipeline/run-01 aborted (21:57 UTC+7):** a Firecrawl job finished without sending any request through
  the tunnel (5 such jobs occurred later in run-02, probably answered from Firecrawl's cache). The client then
  failed on a missing `first_request_at` and re-polled the finished job until the 600 s poll timeout, which
  would have discarded its links. The run was stopped, the bug fixed (`tools/firecrawl_client.py`, regression
  test `test_client_keeps_links_of_a_job_that_sent_no_request`) and the series resumed
  (`output/bench_series_maze_resume.sh`). The run's data is kept and excluded from all statistics.
- **full-pipeline/run-03 aborted (23:29):** Firecrawl credits ran out (HTTP 402, balance 11) in stage 2, job
  12 of 23. Kept, excluded. Firecrawl only run 2 could not be started for the same reason. Both remaining
  runs were made on 2026-10-27 with `output/bench_series_maze_part4.sh` (new API key, new tunnel, same
  code and overrides; log `output/bench_series_maze_part4.out`): firecrawl-only/run-02 and full-pipeline/run-04.
- **Order:** planned A B C D E E D C B A. Actual: A B C D (E aborted) E (E aborted), then C B A
  (`output/bench_series_maze_part3.sh`), then D E on 2026-10-27 (part 4, 3.5 days after the other runs; same
  code, containers restarted, new tunnel URL).
- **Access-log parsing fixed after the first two runs:** Flask colours non-200 status lines, which the first
  parser missed. Those two runs (ZAP only and Wapiti only run 1) were rescored from their saved logs with
  `--recompute`; their crawl data is unchanged.
- **Firecrawl only run 1:** the first job timed out after the 600 s poll limit while Firecrawl was still
  processing it (its links were never returned, though it made 499 requests on the tunnel); rule R1 retried it,
  and the retry job returned the 172 links that produced the result. This is the configured behaviour
  (`poll_timeout_seconds` 600, `max_retries` 1), not a fault of the harness.
- **Firecrawl credits per run are not measurable:** the API's `creditsUsed` is missing from many job
  responses (and from timed-out jobs); saved responses account for 962 of the 1,526 credits spent
  (1,547 → 21). Part 4 (firecrawl-only/run-02 + full-pipeline/run-04) used 837 credits (balance 1,025 →
  188); full-pipeline/run-04's responses report 782 (2 of 34 jobs without a value), firecrawl-only/run-02's
  single job reports none.
- **AngularJS hash-route trap (full pipeline):** `/javascript/frameworks/angularjs` produces ever longer
  hash routes (`#/!/#%2F!%2F%23%2F!%2F…`). The Client Spider and Firecrawl each returned one level more per
  round, so iterations 5–11 found 2–3 new endpoints per round, none of them expected resources, until the
  Client Spider stopped producing new variants and the run saturated after 12 iterations. The normalizer has
  no guard against repeated route segments. No expected resource was found after iteration 3; iterations 4–12
  took 1,019 s (17 min) of the 69.3 min run (sum of iteration wall times).
  In full-pipeline/run-04 the same trap ran longer: 18 iterations, no expected resource after iteration 3,
  iterations 4–18 took 1,547 s (26 min) of the 68.1 min run; Client Spider, Wapiti and Firecrawl kept getting 1–6 trap seeds per round
  and Wapiti re-crawled the whole site each time (246 results, about 97 s, 0 new) until no tool returned a
  new variant.
- **full-pipeline/run-04 startup 409 s:** one Firecrawl job (iteration 14, 1 seed) waited 312 s between
  submission and its first request on the tunnel (Firecrawl-side queueing); this one job accounts for most of
  the difference to run-02's 119 s. 4 of its 34 jobs sent no request through the tunnel (as in run-02)."""


def main():
    for r in DATA["Full pipeline"]:
        write_iteration_report(r)
    sections = [
        "# Security Crawl Maze: 5 configurations × 2 runs", "",
        "Generated by `output/consolidate_maze.py`; every value is computed from the run folders under "
        "`output/benchmarks/crawl-maze/`. Runs were made by `output/bench_series_maze.sh` and its "
        "continuations `_resume`, `_part3`, `_part4` (logs `output/bench_series_maze*.out`).", "",
        "## Comparison", "",
        "Values are `run 1 / run 2`, with the mean when they differ. n = 2 per configuration, so no standard "
        "deviation or confidence interval is given: two runs cannot estimate spread.", "",
        comparison_table(), "",
        "## How to read the table", "", NOTES, "",
        "## Runs", "", run_list(), "",
        "## Full pipeline: iterations", "",
    ]
    for r in DATA["Full pipeline"]:
        sections += [f"### {r['name']}", "", run_summary(r), "", COMPACT_HEAD, *iteration_rows(r, compact=True), "",
                     f"Full detail (input URL lists, parameters, every output URL): "
                     f"`output/benchmarks/crawl-maze/full-pipeline/{r['name']}/iterations.md`.", ""]
    sections += ["## Deviations from the planned methodology", "", DEVIATIONS, ""]
    if ABORTED:
        sections += ["## Aborted runs (kept on disk, excluded from all statistics)", ""]
        for a in ABORTED:
            sections.append(f"- `{a['run']}`: started {a['started']}; finished tool runs before the stop: "
                            f"{', '.join(a['tool_runs_finished']) or 'none'}; Firecrawl job responses saved: "
                            f"{a['firecrawl_jobs_saved']}.")
        sections.append("")
    sections += ["## Expected resources found per configuration (runs reporting it / runs)", "", resource_table(), "",
                 "## ZAP add-on versions in these runs", "", addons() or "–", "",
                 "## Checks", ""]
    sections += [f"- {'PASS' if ok else 'FAIL'}: {desc}" + (f" ({detail})" if not ok else "") for desc, ok, detail in CHECKS]
    passed = sum(ok for _, ok, _ in CHECKS)
    sections += ["", f"{passed}/{len(CHECKS)} checks pass."]
    (ROOT / "series-summary.md").write_text("\n".join(sections) + "\n", encoding="utf-8")
    print(f"{passed}/{len(CHECKS)} checks pass")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
