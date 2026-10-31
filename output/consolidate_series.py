"""
Consolidates the Crawlground series (ZAP only, Wapiti only, ZAP + Wapiti, Firecrawl only, full pipeline)
into output/benchmarks/crawlground/series-summary.md.

Every number in the summary is computed here from the run folders (metrics.json, state.db,
run_events.jsonl, server_results.json, orchestrator.log). The script also cross-checks the stored
metrics against the raw data and lists the result of every check at the end of the summary;
it exits with status 1 if any check fails.

  PYTHONPATH=. .venv/bin/python3 output/consolidate_series.py

output/benchmarks/crawlground/marker_in_source.json (is each marker URL in its test page's initial
HTML/inline scripts?) was generated from the Crawlground image without starting the server:

  docker run --rm --entrypoint node benchmarks-crawlground -e '
    const reg = require("./lib/registry.js").loadRegistry(require("path").join(process.cwd(), "tests"));
    const out = {};
    for (const t of Object.values(reg.byId)) {
      const scoreUrl = `/score/${t.category}/${t.localId}`;
      const r = typeof t.render === "function" ? t.render({ scoreUrl, req: { query: {}, headers: {} } }) : t.render;
      let html = typeof r === "string" ? r : ((r && r.html) || "") + ((r && r.head) || "");
      if (t.head) html += typeof t.head === "function" ? t.head({ scoreUrl }) : t.head;
      out[t.id] = html.includes(scoreUrl);
    }
    console.log(JSON.stringify(out));'
"""
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from evaluation.crawl_metrics import _ts, aggregate, load_ground_truth, load_run, run_metrics

ROOT = Path("output/benchmarks/crawlground")
GT = load_ground_truth("evaluation/ground_truth/crawlground.json")   # match mode "path"
GT_BY_HASH = {e["canonical_hash"]: e for e in GT}
GT_BY_PATH = {e["path_template"]: e["canonical_hash"] for e in GT}
IN_SOURCE = json.loads((ROOT / "marker_in_source.json").read_text())["tests"]   # "cat.id" -> bool
CONFIGS = [("ZAP only", ["zap-only"]), ("Wapiti only", ["wapiti-only"]), ("ZAP + Wapiti", ["zap-wapiti"]),
           ("Firecrawl only", ["firecrawl-only"]), ("Full pipeline", ["full-pipeline", "full-pipeline-b"])]
EXPECTED_RUNS = {"ZAP only": 4, "Wapiti only": 4, "ZAP + Wapiti": 3, "Firecrawl only": 4, "Full pipeline": 3}
# Spread too large for a t-interval (it leaves [0, 1]): report median [min–max] instead
ROBUST = {"Firecrawl only"}
SMALL = set()   # configurations with n = 2 would be reported as mean [min–max]; none at present
FC_VERIFY_RUN = "firecrawl-only/run-04"   # the run with the tunnel traffic log and saved responses
ZAP_ADDONS = {}
for meta_file in sorted(ROOT.glob("*/run-*/run_meta.json")):
    for k, v in json.loads(meta_file.read_text()).get("zap_addons", {}).items():
        ZAP_ADDONS.setdefault(k, set()).add(v)
ZAP_ADDONS = {k: "/".join(sorted(v)) for k, v in ZAP_ADDONS.items() if k != "error"}
TOOL_LABEL = {"zap_traditional_spider": "ZAP traditional spider", "zap_client_spider": "ZAP Client Spider",
              "wapiti_form_crawl": "Wapiti", "firecrawl_navigate": "Firecrawl", "http_probe": "HTTP probe",
              "initial_seed": "initial seed"}
SUMMARY_KEYS = [("recall", "Recall"), ("true_positives", "Markers reported"),
                ("server_markers_scored", "Markers requested (server)"), ("discovered", "Discovered endpoints"),
                ("precision", "Precision"), ("unique_valid", "Unique valid endpoints"),
                ("duplicate_rate", "Duplicate rate"), ("time_to_saturation_s", "Time to saturation (min)"),
                ("t50_s", "t50 (min)"), ("t90_s", "t90 (min)"),
                ("startup_overhead_s", "Tool startup overhead (s)"), ("crawl_time_s", "Crawl time after startup (min)"),
                ("startup_share", "Startup share of tool time"), ("zap_restart_s", "ZAP container restart before the run (s)"),
                ("requests", "Requests (excl. Firecrawl)"),
                ("tool_runs", "Tool runs"), ("failed_runs", "Failed tool runs"), ("timeout_runs", "Timed-out tool runs")]
TIME_KEYS = {"time_to_saturation_s", "t50_s", "t90_s", "crawl_time_s"}
SECOND_KEYS = {"startup_overhead_s", "zap_restart_s"}

checks = []


def check(desc, ok, detail=""):
    checks.append((desc, bool(ok), detail))


RATIO_KEYS = {"recall", "precision", "duplicate_rate", "startup_share"}


def num(value, key, sd=False):
    """Ratios: 4 decimals. Times: minutes (1 decimal, SD 2). Counts: integers, or 1 decimal for means/SDs."""
    if value is None:
        return "–"
    if key in RATIO_KEYS:
        return f"{value:.4f}"
    if key in TIME_KEYS:
        return f"{value / 60:.2f}" if sd else f"{value / 60:.1f}"
    if key in SECOND_KEYS:
        return f"{value:,.1f}"
    if float(value).is_integer() and not sd:
        return f"{int(value):,}"
    return f"{value:,.1f}"


def marker(h):
    return GT_BY_HASH[h]["path_template"].split("/score/")[1]


def load(run_dir, label):
    name = f"{label}/{run_dir.name}"
    m = json.loads((run_dir / "metrics.json").read_text())
    raw = load_run(str(run_dir))
    hist, endpoints, events = raw["history"], raw["endpoints"], raw["events"]
    db = sqlite3.connect(run_dir / "state.db")
    sources = db.execute("SELECT rowid, canonical_hash, source_tool FROM endpoint_sources ORDER BY rowid").fetchall()
    first_iter = dict(db.execute("SELECT canonical_hash, first_seen_iteration FROM endpoints"))
    db.close()

    # an endpoint finds a marker when its path is the marker's path (any method, any query string)
    to_marker = {e["canonical_hash"]: GT_BY_PATH[e["path_template"]] for e in endpoints
                 if e["path_template"] in GT_BY_PATH}
    reported = defaultdict(set)              # marker hash -> tools that reported it
    first_reporter = {}                      # marker hash -> (first tool, its endpoint) in insertion order
    tool_endpoints = defaultdict(set)        # tool -> distinct endpoints it reported
    for _, h, tool in sources:
        tool_endpoints[tool].add(h)
        if h in to_marker:
            reported[to_marker[h]].add(tool)
            first_reporter.setdefault(to_marker[h], (tool, h))
    first_tool = {mh: tool for mh, (tool, _) in first_reporter.items()}

    # the earlier rule: exact canonical key (a marker reported only with a query string or as POST did not count)
    reported_by_key = set(GT_BY_HASH) & {e["canonical_hash"] for e in endpoints}
    check(f"{name}: markers matched by key are a subset of markers matched by path", reported_by_key <= set(reported))

    # Crawlground's own record: markers requested under this run's tool name
    server = json.loads((run_dir / "server_results.json").read_text())
    prefix = f"{label}-run{run_dir.name.split('-')[1]}-"
    names = [t["name"] for t in server["summary"]["tools"] if t["name"].startswith(prefix)]
    check(f"{name}: exactly one Crawlground tool name for this run", len(names) == 1, str(names))
    tool_name = names[0]
    requested = {GT_BY_PATH["/score/" + t["id"].replace(".", "/", 1)] for t in server["tests"]
                 if t["tools"].get(tool_name, {}).get("scored")}
    scored = next(t["scored"] for t in server["summary"]["tools"] if t["name"] == tool_name)
    first_hit = {GT_BY_PATH["/score/" + t["id"].replace(".", "/", 1)]: _ts(t["tools"][tool_name]["firstHit"])
                 for t in server["tests"] if t["tools"].get(tool_name, {}).get("firstHit")}

    # Firecrawl jobs per Firecrawl tool run, from "[FIRECRAWL] Job k/N" log lines
    jobs = []
    for k, n in re.findall(r"\[FIRECRAWL\] Job (\d+)/(\d+):", (run_dir / "orchestrator.log").read_text()):
        if k == "1":
            jobs.append([int(n), 0])
        jobs[-1][1] += 1
    fc_runs = [h for h in hist if h["chosen_tool"] == "firecrawl_navigate"]
    check(f"{name}: one job group per Firecrawl tool run, each complete",
          len(jobs) == len(fc_runs) and all(n == seen for n, seen in jobs), f"{jobs} vs {len(fc_runs)} run(s)")
    job_iter = iter(n for n, _ in jobs)
    for h in hist:
        h["jobs"] = next(job_iter) if h["chosen_tool"] == "firecrawl_navigate" else None
        # markers first found by this step (each tool runs at most once per round)
        h["new_markers"] = sum(1 for tool, eh in first_reporter.values()
                               if tool == h["chosen_tool"] and first_iter[eh] == h["iteration"])

    # ---- consistency checks against the raw data
    recomputed = run_metrics(endpoints=endpoints, gt=GT, history=hist, events=events)
    # measured outside state.db: server record, tunnel log, ZAP restart
    outside = {"server_markers_scored", "zap_restart_s", "firecrawl_requests_observed", "firecrawl_paths_observed"}
    stored = {k: v for k, v in m.items() if k not in outside}
    diff = [k for k in stored if stored[k] != recomputed.get(k)]
    check(f"{name}: metrics.json equals metrics recomputed from state.db + run_events.jsonl", not diff, str(diff))
    tp = set(to_marker.values())
    check(f"{name}: markers reported = markers whose path an endpoint has = markers in endpoint_sources",
          m["true_positives"] == len(tp) == len(reported), f"{m['true_positives']}, {len(tp)}, {len(reported)}")
    check(f"{name}: recall = reported / 59, precision = endpoints on a marker path / discovered",
          m["recall"] == round(len(tp) / len(GT), 4) and m["discovered"] == len(endpoints)
          and m["precision"] == round(len(to_marker) / len(endpoints), 4))
    check(f"{name}: requests = Σ requests_sent over the tool runs", m["requests"] == sum(h["requests_sent"] for h in hist))
    check(f"{name}: discovered = 1 initial seed + Σ new endpoints over the tool runs",
          m["discovered"] == 1 + sum(h["new_endpoints_discovered"] for h in hist))
    check(f"{name}: every tool run's endpoints_after − endpoints_before = its new endpoints",
          all(h["endpoints_after"] - h["endpoints_before"] == h["new_endpoints_discovered"] for h in hist))
    check(f"{name}: markers first found per step add up to markers reported",
          sum(h["new_markers"] for h in hist) == len(tp))
    check(f"{name}: server count = scored markers in results.json = summary count = markers with a first hit",
          m["server_markers_scored"] == len(requested) == scored == len(first_hit), f"{m['server_markers_scored']}, {len(requested)}, {scored}")
    started = next(e for e in events if e["event"] == "run_started")
    finished = next(e for e in events if e["event"] == "run_finished")
    # time windows of the Firecrawl tool runs (tool_run events are written right after a run's ingestion)
    fc_windows = [(e.get("started_at") or _ts(e["ts"]).timestamp() - e["tool_duration_seconds"], _ts(e["ts"]).timestamp())
                  for e in events if e["event"] == "tool_run" and e["chosen_tool"] == "firecrawl_navigate"]
    requested_by_fc = {h for h, t in first_hit.items() if any(a <= t.timestamp() <= b for a, b in fc_windows)}
    secs = round((_ts(finished["ts"]) - _ts(started["ts"])).total_seconds(), 1)
    check(f"{name}: run ended by saturation; time = run_finished − run_started",
          finished.get("termination") == "saturation" and secs == m["time_to_saturation_s"],
          f"{finished.get('termination')}, {secs}s")
    first_stored = {}
    for e in endpoints:
        if e["canonical_hash"] in to_marker:
            mh = to_marker[e["canonical_hash"]]
            first_stored[mh] = min(first_stored.get(mh, _ts(e["created_at"])), _ts(e["created_at"]))
    times = sorted((t - _ts(started["ts"])).total_seconds() for t in first_stored.values())
    t = lambda f: round(max(0.0, times[math.ceil(f * len(times)) - 1]), 1) if times else None
    check(f"{name}: t50 / t90 recomputed from endpoints.created_at", (m["t50_s"], m["t90_s"]) == (t(0.5), t(0.9)),
          f"{(m['t50_s'], m['t90_s'])} vs {(t(0.5), t(0.9))}")
    check(f"{name}: all tool runs ended ok", all(h["status"] == "ok" for h in hist))
    return {"name": name, "metrics": m, "history": hist, "reported": reported, "first_reporter": first_tool,
            "requested": requested, "tool_endpoints": tool_endpoints, "termination": finished.get("termination"),
            "fc_jobs": sum(n for n, _ in jobs),
            "requested_by_fc": requested_by_fc, "reported_by_key": reported_by_key}


# ---------------------------------------------------------------------------- load everything
data = {}
for config, labels in CONFIGS:
    runs = []
    for label in labels:
        for d in sorted((ROOT / label).glob("run-*")):
            if (d / "metrics.json").exists():
                runs.append(load(d, label))
    data[config] = runs
    check(f"{config}: expected number of complete runs", len(runs) == EXPECTED_RUNS[config], str([r["name"] for r in runs]))

out = []
w = out.append
w("# Crawlground series: single tools vs ZAP + Wapiti vs Firecrawl only vs full pipeline\n")
w("Benchmark: Crawlground (`zaproxy/crawlground@7c7ba59`), ground truth = 59 markers "
  "(`evaluation/ground_truth/crawlground.json`). Every run had a restarted Crawlground container, a restarted ZAP "
  "container and a fresh Crawlground tool name.\n")
w("| Configuration | Run folders | Dates | Settings |\n|---|---|---|---|")
w("| ZAP only | `zap-only/run-01..04` | 2026-10-23 | Wapiti, Firecrawl, probe disabled (stage 1 = ZAP traditional spider → Client Spider; rule rounds for ZAP only) |")
w("| Wapiti only | `wapiti-only/run-01..04` | 2026-10-23 | ZAP, Firecrawl, probe disabled |")
w("| ZAP + Wapiti | `zap-wapiti/run-01..03` | 2026-10-16 | `tools.firecrawl.enabled=false`, `tools.http_probe.enabled=false` |")
w("| Firecrawl only | `firecrawl-only/run-01..04` | runs 1–3: 2026-10-16, run 4: 2026-10-23 | ZAP, Wapiti, probe disabled; `orchestrator.stages=[[\"firecrawl_navigate\"]]` (seed: base URL only). Run 4 also logged the tunnel traffic and saved the API responses |")
w("| Full pipeline | `full-pipeline/run-01`, `full-pipeline-b/run-01..02` | 2026-10-16 | default `configs/app_config.json` |\n")
w("Deviations from the protocol in `docs/benchmarks.md`:\n")
w("- Fewer runs than the ≥ 5 the protocol asks for: 3 (ZAP + Wapiti, full pipeline), 4 (single tools, Firecrawl only).")
w("- On 2026-10-16 the configurations were run back to back. On 2026-10-23 they were alternated: ZAP only, Wapiti only, "
  "ZAP only, Wapiti only, Firecrawl only (run 4), then ZAP only, Wapiti only, ZAP only, Wapiti only.")
w("- Runs 1–2 of ZAP only and Wapiti only have every metric except a complete tool startup overhead: the "
  "measurement had two bugs, fixed before runs 3–4 (see Limitations). Their startup is shown as not measured, and "
  "runs 3–4 were added so that each single-tool configuration has two runs with it.")
w("- The full pipeline was paused after its first run to switch the Firecrawl API key; the interrupted second attempt "
  "was discarded and the remaining two runs were made under the label `full-pipeline-b`.")
w("- **ZAP add-on versions differ between the two dates** (see Limitations): the 2026-10-23 runs used the Client "
  "Spider add-on `client` 0.34.0 (released 2026-10-21), the 2026-10-16 runs an earlier version.\n")

# ---------------------------------------------------------------------------- definitions
w("## Metric definitions\n")
w("| Metric | Definition | Source |\n|---|---|---|")
for row in [
    ("Markers reported", "Ground-truth markers that at least one tool reported in its results (the orchestrator's record). A marker counts when a reported endpoint has its path, with any method or query string, as Crawlground's server does", "`state.db`: endpoints matched to the ground truth by path"),
    ("Markers requested (server)", "Markers whose URL Crawlground's server received a request for under this run's tool name (the server's record)", "`server_results.json`"),
    ("Recall", "Markers reported / 59", "`metrics.json`"),
    ("Discovered endpoints", "Distinct normalized endpoints in the run's store (markers, test pages, scripts, forms, query variants, plus the initial seed)", "`state.db`: endpoints"),
    ("Precision", "Discovered endpoints on a marker path / discovered endpoints. Low by construction: the ground truth holds only the 59 markers, so every legitimate test page counts against it", "`metrics.json`"),
    ("Unique valid endpoints", "Discovered endpoints with a known status 2xx/3xx/401/403. Firecrawl reports no status codes, so its endpoints never count", "`metrics.json`"),
    ("Duplicate rate", "1 − discovered / raw results of all tool runs (how much tool output was redundant)", "`metrics.json`"),
    ("Time to saturation", "`run_started` → `run_finished`; the run ends when no rule fires", "`run_events.jsonl`"),
    ("t50 / t90", "Time until 50% / 90% of the run's own final marker count had been stored (`endpoints.created_at`). Results are stored when a tool run finishes, so a one-job run has t50 = t90 = its run time", "`state.db`, `run_events.jsonl`"),
    ("Requests", "Σ `requests_sent` over the tool runs. Firecrawl reports none and is stored as 0 (see below)", "`state.db`: execution_history"),
    ("Tool runs / failed / timed out", "Rows in execution_history and those with status `failed` / `timeout`", "`state.db`: execution_history"),
    ("Firecrawl jobs", "Agent jobs started (`[FIRECRAWL] Job k/N`), each with up to 10 seed URLs", "`orchestrator.log`"),
    ("Tool startup overhead", "Per tool run: time from the orchestrator invoking the tool to the tool's first request at the target; summed over the crawl's tool runs. Boundaries per tool below", "`state.db`: execution_history.startup_seconds"),
    ("Crawl time after startup", "Per tool run: tool run duration − its startup overhead; summed over the same tool runs", "`state.db`: execution_history"),
    ("Startup share", "Startup overhead / (startup overhead + crawl time)", "`metrics.json`"),
    ("ZAP container restart", "Runner's `docker compose restart zap` until ZAP's API answers, before the run starts. Environment reset, not part of any tool run; done in every configuration, also when ZAP is disabled", "`run_meta.json`"),
]:
    w("| " + " | ".join(row) + " |")
w("")
w("Per tool run (step tables): **seeds** = URLs given to the tool; **requests** = see below; "
  "**raw results** = URLs the tool returned before normalization and deduplication; **new endpoints** = endpoints not "
  "in the store when the result was ingested; **new markers** = markers first stored by this step. Tools that run in "
  "parallel are ingested in plan order after all of them finish, so in stage 1 \"new\" for Wapiti means "
  "\"not already found by ZAP\".\n")
w("**What `requests` counts per tool:**\n")
w("| Tool | Counted as |\n|---|---|")
w("| ZAP traditional spider, ZAP Client Spider | HTTP messages to the target in ZAP's history during that tool run, including the browser's script and data requests |")
w("| Wapiti | Rows in Wapiti's crawled-paths table (resources crawled; close to, not exactly, its HTTP requests) |")
w("| HTTP probe | One GET per URL |")
w("| Firecrawl | Not reported by the API; stored as 0. The requests of the full pipeline therefore exclude Firecrawl's traffic |\n")
w("**Tool startup overhead: measurement boundaries.** Start = the moment the orchestrator invokes the tool "
  "(`ToolRegistry.run`, the same start as the tool run's duration). End = the tool's first request at the target, as "
  "observed for each tool below. Everything in between is startup: process or browser launch, API calls that "
  "configure the scan, the cloud job queue. Everything after it is crawl time. Each tool run is measured once: later "
  "browser launches inside the same run (Client Spider: one scan per seed; Firecrawl: one job per 10 seeds) are crawl time.\n")
w("| Tool | First request observed as | Note |\n|---|---|---|")
w("| ZAP traditional spider, Client Spider | Earliest `timestamp` (time sent) of the run's messages to the target in ZAP's history | "
  "The ZAP daemon is already running: its start is the separate \"ZAP container restart\" |")
w("| Wapiti | First `[+] <METHOD> <url>` line of Wapiti's output, timed when the orchestrator reads it | Wapiti prints the "
  "line after fetching the resource, so this includes one local round trip (milliseconds); includes Python start-up "
  "and, in headless mode, launching Firefox and Wapiti's mitmproxy |")
w("| Firecrawl | First request in the tunnel traffic log after the job was submitted | Includes the API call, the cloud "
  "queue and the cloud browser start; only for runs with `--tunnel-proxy` |")
w("| HTTP probe | When the first GET is sent (no setup) | |\n")
w("A tool run that sends no request (e.g. a rule round whose seeds were all known) has no startup value and is "
  "left out of both sums.\n")

# ---------------------------------------------------------------------------- summary
w("## Summary over the runs\n")
w("ZAP only, Wapiti only, ZAP + Wapiti and the full pipeline: mean ± sample SD [95% t-interval]. A zero SD means the "
  "runs were identical, and the interval collapses to one value. A metric measured in only some runs of a "
  "configuration is shown as median [min–max] with \"n = k of N\". **Firecrawl only: median [min–max]**, because its spread is so "
  "large that a t-interval is meaningless (for recall it would be "
  f"[{aggregate([r['metrics'] for r in data['Firecrawl only']])['recall']['ci95'][0]}, "
  f"{aggregate([r['metrics'] for r in data['Firecrawl only']])['recall']['ci95'][1]}], outside [0, 1]). "
  "Per-run values are in the next table.\n")
w("| Metric | " + " | ".join(f"{c} (n = {len(data[c])})" for c, _ in CONFIGS) + " |")
w("|---|" + "---|" * len(CONFIGS))
aggs = {c: aggregate([r["metrics"] for r in data[c]]) for c, _ in CONFIGS}
def values(c, key):
    """Per-run values of a metric; runs from before a metric existed have none (None/missing)."""
    return [r["metrics"].get(key) for r in data[c] if r["metrics"].get(key) is not None]


for c, _ in CONFIGS:
    for key, _ in SUMMARY_KEYS:
        vals = values(c, key)
        if len(vals) >= 2:
            a = aggs[c][key]
            check(f"{c}: {key} mean/SD equal statistics.mean/stdev",
                  a["mean"] == round(statistics.mean(vals), 4) and a["sd"] == round(statistics.stdev(vals), 4))
for key, label in SUMMARY_KEYS:
    cells = []
    for c, _ in CONFIGS:
        vals = values(c, key)
        part = f" (n = {len(vals)} of {len(data[c])})" if 0 < len(vals) < len(data[c]) else ""
        if not vals:
            cells.append("not measured")
        elif len(vals) == 1:
            cells.append(f"{num(vals[0], key)}{part}")
        elif c in ROBUST or part:
            cells.append(f"{num(statistics.median(vals), key)} [{num(min(vals), key)}–{num(max(vals), key)}]{part}")
        elif c in SMALL:
            cells.append(f"{num(statistics.mean(vals), key)} [{num(min(vals), key)}–{num(max(vals), key)}]")
        else:
            a = aggs[c][key]
            cells.append(f"{num(a['mean'], key)} ± {num(a['sd'], key, sd=True) if a['sd'] else '0'} "
                         f"[{num(a['ci95'][0], key)}, {num(a['ci95'][1], key)}]")
    w(f"| {label} | " + " | ".join(cells) + " |")
w("\n\"not measured\": the metric did not exist yet when these runs were made (tool startup overhead and the ZAP "
  "restart time are recorded from 2026-10-23 on). Startup \"n = 2 of 4\" for the single tools: runs 1–2 were measured "
  "incompletely (see Deviations). t50/t90 \"n = 3 of 4\" for Firecrawl only: run 4 reported no marker, so it has no t50/t90.")
fc_recall = [r["metrics"]["recall"] for r in data["Firecrawl only"]]
w(f"\nFirecrawl-only recall per run: {', '.join(num(v, 'recall') for v in fc_recall)} "
  f"→ median {num(statistics.median(fc_recall), 'recall')} "
  f"({', '.join(str(r['metrics']['true_positives']) for r in data['Firecrawl only'])} of 59 markers).\n")

# ---------------------------------------------------------------------------- findings (computed)
zw, fo, fp = data["ZAP + Wapiti"], data["Firecrawl only"], data["Full pipeline"]
zw_found = set().union(*(set(r["reported"]) for r in zw))
hard = set(GT_BY_HASH) - zw_found
fo_hard_reported = sum(len(hard & set(r["reported"])) for r in fo)
fo_hard_both = sum(len(hard & set(r["reported"]) & r["requested"]) for r in fo)
fp_stage2 = [sum(h["new_markers"] for h in r["history"] if h["chosen_tool"] == "firecrawl_navigate") for r in fp]
fp_fc_first = [sum(1 for h in hard if r["first_reporter"].get(h) == "firecrawl_navigate") for r in fp]
fp_fc_min = [sum(h["tool_duration_seconds"] for h in r["history"] if h["chosen_tool"] == "firecrawl_navigate") / 60
             for r in fp]
t_zw, t_fp = aggs["ZAP + Wapiti"]["time_to_saturation_s"]["mean"], aggs["Full pipeline"]["time_to_saturation_s"]["mean"]
w("## Findings\n")
w(f"1. **ZAP + Wapiti** reported {', '.join(str(r['metrics']['true_positives']) for r in zw)} markers: identical runs, "
  f"and the same {len(hard)} markers are missing every time (listed below).")
fp_fc_req = [len(hard & r["requested_by_fc"]) for r in fp]
w(f"2. **Full pipeline** reported {', '.join(str(r['metrics']['true_positives']) for r in fp)} markers. In every run "
  f"Firecrawl was the first to report all of those {len(hard)} markers ({', '.join(map(str, fp_fc_first))} per run), "
  f"in its stage-2 run. The server's first-hit times show that Firecrawl itself requested "
  f"{', '.join(map(str, fp_fc_req))} of them during that run; the others were first requested when round 3 started, "
  "by the HTTP probe and the ZAP Client Spider seeded with Firecrawl's URLs. That is why the server saw all 59.")
w(f"3. **Firecrawl only** reported {', '.join(str(r['metrics']['true_positives']) for r in fo)} markers, but the "
  f"server received requests for only {', '.join(str(r['metrics']['server_markers_scored']) for r in fo)}. Of its "
  f"{fo_hard_reported} reports of the {len(hard)} hard markers (over the {len(fo)} runs), only {fo_hard_both} came with a "
  "request for that marker. So in runs 1–3 Firecrawl most likely got these URLs by reading the page source rather than by "
  "triggering the hover, focus, scroll or frame behaviour; in the pipeline, other tools then request what it reports. "
  f"Run 4 shows the opposite failure: its browser requested {fo[-1]['metrics']['server_markers_scored']} markers, but its "
  "answer listed none of them (see the verification section).")
w(f"4. **Cost:** the full pipeline takes {t_fp / t_zw:.1f}× as long as ZAP + Wapiti "
  f"({t_fp / 60:.1f} vs {t_zw / 60:.1f} min on average). The Firecrawl steps account for "
  f"{', '.join(f'{x:.1f}' for x in fp_fc_min)} min per run; Firecrawl's requests are not measured.")
zo, wo = data["ZAP only"], data["Wapiti only"]
zo_found = set().union(*(set(r["reported"]) for r in zo))
wo_found = set().union(*(set(r["reported"]) for r in wo))
w(f"5. **Single tools.** ZAP alone reported {', '.join(str(r['metrics']['true_positives']) for r in zo)} markers, "
  f"Wapiti alone {', '.join(str(r['metrics']['true_positives']) for r in wo)}. Over their runs, {len(zo_found - wo_found)} "
  f"markers were reported only by ZAP, {len(wo_found - zo_found)} only by Wapiti, and their union is "
  f"{len(zo_found | wo_found)} (ZAP + Wapiti together: {len(zw_found)}). "
  f"{len(hard & (zo_found | wo_found))} of the {len(hard)} markers ZAP + Wapiti never reported were reported by a single-tool run.")


def tool_startups(runs, tool):
    return [h["startup_seconds"] for r in runs for h in r["history"]
            if h["chosen_tool"] == tool and h.get("startup_seconds") is not None]


rows = []
for c, _ in CONFIGS:
    for tool in TOOL_LABEL:
        st = tool_startups(data[c], tool)
        if st:
            rows.append(f"{TOOL_LABEL[tool]} in {c}: median {statistics.median(st):.1f} s over {len(st)} tool run(s)")
measured = [(c, values(c, "startup_share")) for c, _ in CONFIGS if values(c, "startup_share")]
w("6. **Tool startup overhead** (runs from 2026-10-23): "
  + ("; ".join(f"{c}: {min(v):.1%}" + (f"–{max(v):.1%}" if max(v) != min(v) else "") + " of the tool time"
               for c, v in measured) or "no measured runs")
  + ". Per tool run: " + ("; ".join(rows) or "none") + ". The ZAP container restart before each run (not part of "
  "any tool run) took " + (lambda v: f"{min(v):.1f}–{max(v):.1f} s over {len(v)} runs" if v else "–")(
      [v for c, _ in CONFIGS for v in values(c, "zap_restart_s")]) + ".\n")


# ---------------------------------------------------------------------------- limitations (computed)
in_src = lambda h: IN_SOURCE[marker(h).replace("/", ".", 1)]
check("marker_in_source.json covers exactly the 59 ground-truth markers",
      {marker(h).replace("/", ".", 1) for h in GT_BY_HASH} == set(IN_SOURCE))
not_in_src = sorted(marker(h) for h in GT_BY_HASH if not in_src(h))
w("## Limitations of these measurements\n")
w("1. **A marker counts on any request to its URL, not on the interaction.** Crawlground's `handleScore` "
  "(`server.js`) marks a test as scored for any GET or POST to `/score/<category>/<test>`; no test in this version "
  "sets a `scoreWhen` condition. A direct GET by the HTTP probe scores exactly like a hover or a click. "
  "\"Requested (server)\" therefore means the URL was fetched by some means.")
w(f"2. **Most marker URLs are visible in the page source.** {sum(IN_SOURCE.values())} of {len(IN_SOURCE)} test pages "
  "contain their marker URL literally in the initial HTML or an inline script (all except "
  + ", ".join(f"`{m}`" for m in not_in_src) + "; `marker_in_source.json`). "
  f"All {len(hard)} markers that ZAP + Wapiti miss are among them. A tool that reads the source can report these "
  "markers without performing the hover, scroll or click the test is about, so neither \"reported\" nor "
  "\"requested\" proves an interaction happened.")
diffs = [(c, r) for c, _ in CONFIGS for r in data[c] if len(r["reported_by_key"]) != len(r["reported"])]
w("3. **Markers are matched by path.** A submitted form is reported as e.g. "
  "`/score/forms/04-select-field?colour=green` or as a POST, which normalizes to a different canonical key than the "
  "marker. Crawlground scores those requests anyway, so the ground truth matches by path (`\"match\": \"path\"` in "
  "`crawlground.json`). Earlier versions of these tables matched by exact key; runs where that gave a lower count: "
  + ("; ".join(f"`{r['name']}` {len(r['reported_by_key'])} → {len(r['reported'])}" for c, r in diffs) or "none")
  + ". Precision is higher than before in every run, because query and POST variants of a marker now count as "
  "matching endpoints.")
w("4. **Firecrawl's own traffic is observed only in run 4.** In runs 1–3 its requests are not counted (only "
  "Crawlground's marker hits are visible), and the client kept only the `links` of each job, not the `actions_taken` "
  "list, so which pages Firecrawl loaded there is unknown. Run 4 recorded both (tunnel traffic log, saved responses; "
  "see the verification section below).")
w("5. **ZAP add-on versions changed between the two dates.** ZAP checks for add-on updates whenever its container is "
  "created, so the versions are not pinned by the image tag. The 2026-10-23 runs record them in `run_meta.json`: "
  + ", ".join(f"`{k}` {v}" for k, v in sorted(ZAP_ADDONS.items())) + ". The Client Spider add-on `client` 0.34.0 "
  "was released on 2026-10-21, so the 2026-10-16 runs used an earlier version (0.33.0 was the current one then; "
  "not recorded). Differences between ZAP only (2026-10-23) and ZAP + Wapiti (2026-10-16) can therefore come from "
  "the add-on version as well as from Wapiti.")
w("6. **Tool startup overhead exists only for the 2026-10-23 runs.** The earlier runs did not record the tool's first "
  "request, and the orchestrator log has no per-line timestamps, so it cannot be reconstructed. Comparisons of "
  "startup overhead are between ZAP only, Wapiti only and Firecrawl-only run 4. Firecrawl's startup includes the "
  "cloud queue and network latency through the tunnel, which the local tools do not have.")
w("7. **The startup measurement was fixed after the first single-tool runs.** In ZAP-only runs 1–2 the Client Spider "
  "and the second traditional-spider run got no value: ZAP's history offset taken at the start of a tool run can "
  "include one message of the previous tool run that was stored late, which made the \"first request\" earlier than "
  "the run itself and it was discarded. Messages sent before the tool run started are now ignored for startup. In "
  "Wapiti-only runs 1–2 nothing was measured, because Wapiti writes its `[+] <METHOD> <url>` lines only to its "
  "`--log` file, not to stdout; the log file is now watched (every 0.1 s). A run whose tool runs lack a value is "
  "reported as not measured, never as a partial sum.")
w("8. **Requests of ZAP tool runs after the first can include one leftover message** from the previous tool run, for "
  "the same reason. This applies equally to the 2026-10-16 and 2026-10-23 runs and was left unchanged to keep "
  "request counts comparable; it is at most one request per ZAP tool run.\n")
w("Firecrawl only (Firecrawl is the only tool in these runs, so every request the server saw was Firecrawl's):\n")
w("| Run | Reported | Requested | Both | Reported, not requested | …of which URL is in the page source "
  "| Requested, not reported |")
w("|---|---|---|---|---|---|---|")
for r in fo:
    rep, req = set(r["reported"]), r["requested"]
    w(f"| `{r['name']}` | {len(rep)} | {len(req)} | {len(rep & req)} | {len(rep - req)} "
      f"| {sum(map(in_src, rep - req))} | {len(req - rep)}" + (": " + ", ".join(f"`{marker(h)}`" for h in sorted(req - rep, key=marker)) if req - rep else "") + " |")
w("")

# ---------------------------------------------------------------------------- Firecrawl run 4 verification
from urllib.parse import urlparse   # noqa: E402

w(f"## Verification of Firecrawl's reported URLs (`{FC_VERIFY_RUN}`)\n")
vdir = ROOT / FC_VERIFY_RUN
traffic_file = vdir / "tunnel_traffic.jsonl"
if not traffic_file.exists():
    w("Not available: the run has no tunnel traffic log.\n")
else:
    vrun = next(r for r in data["Firecrawl only"] if r["name"] == FC_VERIFY_RUN)
    traffic = [json.loads(line) for line in traffic_file.read_text().splitlines() if line.strip()]
    responses = sorted((json.loads(f.read_text()) for f in (vdir / "firecrawl_responses").glob("*.json")),
                       key=lambda d: d.get("job", 0))
    links = [l for d in responses for l in (((d.get("response") or {}).get("data") or {}).get("links") or [])]
    actions = [a for d in responses for a in (((d.get("response") or {}).get("data") or {}).get("actions_taken") or [])]
    path_of = lambda u: urlparse(u).path.rstrip("/") or "/"
    marker_paths = {e["path_template"]: h for h, e in GT_BY_HASH.items()}
    reported = {marker_paths[path_of(l)] for l in links if path_of(l) in marker_paths}
    req_by_marker = defaultdict(list)
    for t in traffic:
        mp = path_of(t["path"])
        if mp in marker_paths:
            req_by_marker[marker_paths[mp]].append(t)
    requested = set(req_by_marker)
    server = json.loads((vdir / "server_results.json").read_text())
    tname = json.loads((vdir / "run_meta.json").read_text())["crawlground_tool_name"]
    hits = {GT_BY_PATH["/score/" + t["id"].replace(".", "/", 1)]: t["tools"][tname]["hits"]
            for t in server["tests"] if t["tools"].get(tname, {}).get("scored")}
    check(f"{FC_VERIFY_RUN}: markers requested through the tunnel = markers Crawlground scored", requested == set(hits),
          f"{sorted(map(marker, requested ^ set(hits)))}")
    check(f"{FC_VERIFY_RUN}: requests to marker paths in the tunnel log = Crawlground's hit count",
          sum(len(v) for v in req_by_marker.values()) == sum(hits.values()),
          f"{sum(len(v) for v in req_by_marker.values())} vs {sum(hits.values())}")
    check(f"{FC_VERIFY_RUN}: markers in the saved responses = markers reported in state.db", reported == set(vrun["reported"]),
          f"{len(reported)} vs {len(vrun['reported'])}")

    def from_test_page(h, t):
        ref = t.get("referer") or ""
        return path_of(ref) == "/test/" + marker(h) if ref else False

    test_pages = {path_of(t["path"]) for t in traffic if path_of(t["path"]).startswith("/test/")}
    uas = Counter((t.get("user_agent") or "")[:80] for t in traffic)
    st = [d["startup_seconds"] for d in responses if d.get("startup_seconds") is not None]
    w("Run 4 logged every request that reached Crawlground through the tunnel (only Firecrawl uses it) and saved each "
      "job's full API response. Crawlground's README says a test is scored \"when this URL is reached (via the "
      "control)\" and that the marker \"is not linked from anywhere else\"; its code (`handleScore` in `server.js`) "
      "records any GET or POST to the marker path, and no test in this version defines a `scoreWhen` check. So:\n")
    w("- A marker URL in Firecrawl's answer shows only that Firecrawl **knew** the URL (56 of 59 are in the page source).")
    w("- A request in the tunnel log, matching Crawlground's own record, shows Firecrawl's browser **requested** it.")
    w("- Neither shows **how**. The Referer header is a hint: a request sent with its own test page as Referer was made "
      "from inside that page (by the control, by the page's own script, or by a navigation from it); a request without "
      "it was most likely a direct navigation to a URL the agent already had. It is not proof of the interaction.\n")
    w(f"| Measure | Value |\n|---|---|")
    w(f"| Firecrawl jobs | {len(responses)} ({', '.join(d.get('outcome', '?') for d in responses)}) |")
    w(f"| Requests through the tunnel | {len(traffic)} ({len({path_of(t['path']) for t in traffic})} distinct paths, "
      f"{len(test_pages)} distinct test pages) |")
    w(f"| Startup per job (submit → first request) | {', '.join(f'{x:.1f} s' for x in st) or '–'} |")
    w(f"| URLs returned (`links`) | {len(links)} |")
    w(f"| Actions it reported (`actions_taken`) | {len(actions)} |")
    w(f"| Markers reported | {len(reported)} |")
    w(f"| Markers requested (tunnel log) | {len(requested)}; Crawlground scored {len(hits)} with {sum(hits.values())} hit(s) |")
    w(f"| Reported and requested | {len(reported & requested)} |")
    w(f"| Reported, not requested | {len(reported - requested)} (URL in page source: "
      f"{sum(map(in_src, reported - requested))}) |")
    w(f"| Requested, not reported | {len(requested - reported)} |")
    w(f"| Requested with its own test page as Referer | "
      f"{sum(1 for h in requested if any(from_test_page(h, t) for t in req_by_marker[h]))} of {len(requested)} |")
    w(f"| User agents | {'; '.join(f'`{u}` ({n})' for u, n in uas.most_common(3))} |\n")
    w("Per marker (only markers that were reported or requested):\n")
    w("| Marker | URL in page source | Reported | Requested (method, status) | Referer of the first request |")
    w("|---|---|---|---|---|")
    for h in sorted(reported | requested, key=marker):
        reqs = sorted(req_by_marker.get(h, []), key=lambda t: t["ts"])
        how = ", ".join(f"{t['method']} {t['status']}" for t in reqs[:3]) + (" …" if len(reqs) > 3 else "")
        ref = reqs[0].get("referer") if reqs else None
        refcell = "–" if not reqs else ("its test page" if from_test_page(h, reqs[0]) else
                                       (f"`{path_of(ref)}`" if ref else "none"))
        w(f"| `{marker(h)}` | {'yes' if in_src(h) else 'no'} | {'yes' if h in reported else 'no'} "
          f"| {how or 'no'} | {refcell} |")
    w("")
    if actions:
        w(f"Actions Firecrawl reported (`actions_taken`, its own account, not verified):\n")
        for a in actions[:80]:
            w(f"- {str(a)[:500]}")
        if len(actions) > 80:
            w(f"- … {len(actions) - 80} more in `firecrawl_responses/`")
        w("")
    per_cat_req = Counter(GT_BY_HASH[h]["category"] for h in requested)
    w(f"**Claims vs. record.** The tunnel log confirms the page visits Firecrawl describes: it requested "
      f"{len(test_pages)} of the 59 test pages. It does not confirm the interactions: Firecrawl says it clicked marker "
      f"links, submitted the forms and triggered the event, frame and navigation tests, but the server received "
      f"requests for {len(requested)} markers ("
      + ", ".join(f"{c} {n}" for c, n in sorted(per_cat_req.items())) +
      f"), and its answer listed {len(reported)} marker URLs among its {len(links)} links. Either the actions did not "
      "fire the marker requests, or they were not performed as described; the saved responses cannot tell which. "
      "`actions_taken` is therefore not usable as evidence of interaction.\n")

# ---------------------------------------------------------------------------- per run
w("## Per run\n")
w("| Configuration | Run | Markers reported | Requested (server) | Recall | Discovered | Precision | Unique valid "
  "| Duplicate rate | Time (min) | t50 (min) | t90 (min) | Startup (s) | Crawl time (min) | Requests | Tool runs "
  "| Firecrawl jobs | Ended by |")
w("|---|---|" + "---|" * 16)
for c, _ in CONFIGS:
    for r in data[c]:
        m = r["metrics"]
        w(f"| {c} | `{r['name']}` | {m['true_positives']} | {m['server_markers_scored']} | {num(m['recall'], 'recall')} "
          f"| {m['discovered']} | {num(m['precision'], 'precision')} | {m['unique_valid']} | {num(m['duplicate_rate'], 'duplicate_rate')} "
          f"| {num(m['time_to_saturation_s'], 'time_to_saturation_s')} | {num(m['t50_s'], 't50_s')} "
          f"| {num(m['t90_s'], 't90_s')} | {num(m.get('startup_overhead_s'), 'startup_overhead_s')} "
          f"| {num(m.get('crawl_time_s'), 'crawl_time_s')} | {m['requests']:,} | {m['tool_runs']} | {r['fc_jobs']} "
          f"| {r['termination']} |")
w("")

# ---------------------------------------------------------------------------- reported vs requested
w("## Markers reported vs markers requested\n")
w("\"Reported\" is what the tools told the orchestrator; \"requested\" is what Crawlground's server actually received. "
  "A tool can report a URL it only read in the page source (a link, a script's `fetch()` target) without loading it, "
  "and can load a URL without reporting it.\n")
w("| Configuration | Run | Reported | Requested | Both | Reported, not requested | Requested, not reported |")
w("|---|---|---|---|---|---|---|")
for c, _ in CONFIGS:
    for r in data[c]:
        rep, req = set(r["reported"]), r["requested"]
        w(f"| {c} | `{r['name']}` | {len(rep)} | {len(req)} | {len(rep & req)} | {len(rep - req)} | {len(req - rep)} |")
w("\nZAP, Wapiti and the probe report what they requested, so both numbers match whenever they run. In the full "
  "pipeline the markers Firecrawl reported are requested afterwards by the ZAP Client Spider, the traditional spider "
  "or the HTTP probe.\n")

# ---------------------------------------------------------------------------- full pipeline steps
w("## Full pipeline, step by step\n")
w("Stage 1 runs the ZAP lane (traditional spider, then Client Spider) in parallel with Wapiti. Stage 2 runs Firecrawl "
  "on every page-like URL found so far, 10 seeds per job. Rule rounds follow; within a round, tools of different "
  "engines run in parallel. The run ends when no rule fires (saturation). Minutes are each tool run's own duration; "
  "parallel steps overlap, so they do not add up to the run time.\n")
for r in data["Full pipeline"]:
    m = r["metrics"]
    w(f"### `{r['name']}`: {m['true_positives']}/59 markers, {m['discovered']} endpoints, "
      f"{num(m['time_to_saturation_s'], 'time_to_saturation_s')} min\n")
    w("| # | Round | Stage / rule | Tool | Seeds | Firecrawl jobs | Requests | Raw results | New endpoints | New markers | Minutes |")
    w("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, h in enumerate(r["history"], 1):
        req = "unknown" if h["chosen_tool"] == "firecrawl_navigate" else f"{h['requests_sent']:,}"
        w(f"| {i} | {h['iteration']} | {h['rule_id']} | {TOOL_LABEL[h['chosen_tool']]} | {h['seeds_count']} "
          f"| {h['jobs'] or '–'} | {req} | {h['raw_results']} | {h['new_endpoints_discovered']} | {h['new_markers']} "
          f"| {h['tool_duration_seconds'] / 60:.1f} |")
    w(f"| | | **Total** | | | {r['fc_jobs']} | {m['requests']:,} (excl. Firecrawl) "
      f"| {sum(h['raw_results'] for h in r['history']):,} | {m['discovered'] - 1} (+1 initial seed) "
      f"| {sum(h['new_markers'] for h in r['history'])} | |\n")

# ---------------------------------------------------------------------------- per tool (ZAP / Wapiti configs)
w("## ZAP only, Wapiti only, ZAP + Wapiti: per tool\n")
w("Totals over each tool's runs within one crawl. \"Distinct endpoints\" and \"markers\" count everything the tool "
  "reported, including what another tool had already found; \"first found\" counts markers this tool stored first. "
  "Startup and crawl time are summed over the tool's runs (2026-10-23 runs only; \"–\" = not measured).\n")
w("| Run | Tool | Tool runs | Seeds | Requests | Raw results | New endpoints | Distinct endpoints reported "
  "| Markers reported | Markers first found | Startup (s) | Crawl time (min) | Minutes |")
w("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for c in ("ZAP only", "Wapiti only", "ZAP + Wapiti"):
    for r in data[c]:
        totals = Counter()
        for tool in ("zap_traditional_spider", "zap_client_spider", "wapiti_form_crawl"):
            hs = [h for h in r["history"] if h["chosen_tool"] == tool]
            if not hs:
                continue
            markers = sum(1 for tools in r["reported"].values() if tool in tools)
            first = sum(1 for t in r["first_reporter"].values() if t == tool)
            st = [h["startup_seconds"] for h in hs if h.get("startup_seconds") is not None]
            crawl = [h["tool_duration_seconds"] - h["startup_seconds"] for h in hs if h.get("startup_seconds") is not None]
            row = {"runs": len(hs), "seeds": sum(h["seeds_count"] for h in hs),
                   "req": sum(h["requests_sent"] for h in hs), "raw": sum(h["raw_results"] for h in hs),
                   "new": sum(h["new_endpoints_discovered"] for h in hs), "first": first,
                   "min": sum(h["tool_duration_seconds"] for h in hs) / 60}
            totals.update(row)
            w(f"| `{r['name']}` | {TOOL_LABEL[tool]} | {row['runs']} ({' + '.join(h['rule_id'] for h in hs)}) "
              f"| {' + '.join(str(h['seeds_count']) for h in hs)} | {row['req']:,} | {row['raw']:,} | {row['new']} "
              f"| {len(r['tool_endpoints'][tool])} | {markers} | {first} "
              f"| {' + '.join(f'{x:.1f}' for x in st) if st else '–'} | {sum(crawl) / 60:.1f} | {row['min']:.1f} |"
              if st else
              f"| `{r['name']}` | {TOOL_LABEL[tool]} | {row['runs']} ({' + '.join(h['rule_id'] for h in hs)}) "
              f"| {' + '.join(str(h['seeds_count']) for h in hs)} | {row['req']:,} | {row['raw']:,} | {row['new']} "
              f"| {len(r['tool_endpoints'][tool])} | {markers} | {first} | – | – | {row['min']:.1f} |")
        m = r["metrics"]
        check(f"{r['name']}: per-tool totals add up to the run's requests and new endpoints",
              totals["req"] == m["requests"] and totals["new"] + 1 == m["discovered"]
              and totals["first"] == m["true_positives"])
        w(f"| `{r['name']}` | **All tools** | {totals['runs']} | | {totals['req']:,} | {totals['raw']:,} "
          f"| {totals['new']} (+1 initial seed) | {m['discovered']} | {m['true_positives']} | {totals['first']} "
          f"| {num(m.get('startup_overhead_s'), 'startup_overhead_s')} | {num(m.get('crawl_time_s'), 'crawl_time_s')} "
          f"| run time {num(m['time_to_saturation_s'], 'time_to_saturation_s')} |")
w("")

# ---------------------------------------------------------------------------- categories
w("## Markers reported per category (per run: run 1 / run 2 / …)\n")
cats = sorted({e["category"] for e in GT})
w("| Category | Markers | " + " | ".join(c for c, _ in CONFIGS) + " |")
w("|---|---|" + "---|" * len(CONFIGS))
for cat in cats:
    keys = {h for h, e in GT_BY_HASH.items() if e["category"] == cat}
    w(f"| {cat} | {len(keys)} | " + " | ".join(" / ".join(str(len(keys & set(r["reported"]))) for r in data[c])
                                               for c, _ in CONFIGS) + " |")
w("")

zw_union = set().union(*(set(r["reported"]) for r in data["ZAP + Wapiti"]))
never = sorted(set(GT_BY_HASH) - zw_union, key=marker)
w(f"## The {len(never)} markers ZAP + Wapiti never reported\n")
w("| Marker | ZAP only: reported | Wapiti only: reported | Firecrawl only: reported | Firecrawl only: requested "
  "| Full pipeline: reported | Full pipeline: first reported by | Full pipeline: first requested during a Firecrawl run |")
w("|---|---|---|---|---|---|---|---|")
for h in never:
    fo, fp = data["Firecrawl only"], data["Full pipeline"]
    firsts = Counter(TOOL_LABEL[r["first_reporter"][h]] for r in fp if h in r["first_reporter"])
    w(f"| `{marker(h)}` | {sum(h in r['reported'] for r in zo)}/{len(zo)} | {sum(h in r['reported'] for r in wo)}/{len(wo)} "
      f"| {sum(h in r['reported'] for r in fo)}/{len(fo)} | {sum(h in r['requested'] for r in fo)}/{len(fo)} "
      f"| {sum(h in r['reported'] for r in fp)}/{len(fp)} | "
      + ", ".join(f"{t} ({n}/{len(fp)})" for t, n in firsts.items())
      + f" | {sum(h in r['requested_by_fc'] for r in fp)}/{len(fp)} |")
w("")

# ---------------------------------------------------------------------------- verification
failed = [c for c in checks if not c[1]]
w("## Verification\n")
w(f"`output/consolidate_series.py` computes every value above from the run folders and runs these checks: "
  f"**{len(checks) - len(failed)} of {len(checks)} passed**.\n")
groups = defaultdict(list)
for desc, ok, detail in checks:
    groups[re.sub(r"^[^:]*: ", "", desc)].append((desc.split(":")[0], ok, detail))
w("| Check | Runs / configs | Result |\n|---|---|---|")
for desc, items in groups.items():
    bad = [f"{who} ({detail})" for who, ok, detail in items if not ok]
    w(f"| {desc} | {len(items)} | {'✓ all passed' if not bad else '✗ ' + '; '.join(bad)} |")

text = "\n".join(out) + "\n"
(ROOT / "series-summary.md").write_text(text)
print(text)
for desc, ok, detail in failed:
    print(f"FAILED: {desc} {detail}", file=sys.stderr)
sys.exit(1 if failed else 0)
