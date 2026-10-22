"""
Consolidates the Crawlground series (ZAP + Wapiti, Firecrawl only, full pipeline) into
output/benchmarks/crawlground/series-summary.md.

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

from evaluation.crawl_metrics import _ts, aggregate, load_run, run_metrics

ROOT = Path("output/benchmarks/crawlground")
GT = json.loads(Path("evaluation/ground_truth/crawlground.json").read_text())["entries"]
GT_BY_HASH = {e["canonical_hash"]: e for e in GT}
GT_BY_PATH = {e["path_template"]: e["canonical_hash"] for e in GT}
IN_SOURCE = json.loads((ROOT / "marker_in_source.json").read_text())["tests"]   # "cat.id" -> bool
CONFIGS = [("ZAP + Wapiti", ["zap-wapiti"]), ("Firecrawl only", ["firecrawl-only"]),
           ("Full pipeline", ["full-pipeline", "full-pipeline-b"])]
# Spread too large for a t-interval with n = 3 (it leaves [0, 1]): report median [min–max] instead
ROBUST = {"Firecrawl only"}
TOOL_LABEL = {"zap_traditional_spider": "ZAP traditional spider", "zap_client_spider": "ZAP Client Spider",
              "wapiti_form_crawl": "Wapiti", "firecrawl_navigate": "Firecrawl", "http_probe": "HTTP probe",
              "initial_seed": "initial seed"}
SUMMARY_KEYS = [("recall", "Recall"), ("true_positives", "Markers reported"),
                ("server_markers_scored", "Markers requested (server)"), ("discovered", "Discovered endpoints"),
                ("precision", "Precision"), ("unique_valid", "Unique valid endpoints"),
                ("duplicate_rate", "Duplicate rate"), ("time_to_saturation_s", "Time to saturation (min)"),
                ("t50_s", "t50 (min)"), ("t90_s", "t90 (min)"), ("requests", "Requests (excl. Firecrawl)"),
                ("tool_runs", "Tool runs"), ("failed_runs", "Failed tool runs"), ("timeout_runs", "Timed-out tool runs")]
TIME_KEYS = {"time_to_saturation_s", "t50_s", "t90_s"}

checks = []


def check(desc, ok, detail=""):
    checks.append((desc, bool(ok), detail))


RATIO_KEYS = {"recall", "precision", "duplicate_rate"}


def num(value, key, sd=False):
    """Ratios: 4 decimals. Times: minutes (1 decimal, SD 2). Counts: integers, or 1 decimal for means/SDs."""
    if value is None:
        return "–"
    if key in RATIO_KEYS:
        return f"{value:.4f}"
    if key in TIME_KEYS:
        return f"{value / 60:.2f}" if sd else f"{value / 60:.1f}"
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

    reported = defaultdict(set)              # marker hash -> tools that reported it
    first_reporter = {}                      # marker hash -> first tool (insertion order)
    tool_endpoints = defaultdict(set)        # tool -> distinct endpoints it reported
    for _, h, tool in sources:
        tool_endpoints[tool].add(h)
        if h in GT_BY_HASH:
            reported[h].add(tool)
            first_reporter.setdefault(h, tool)

    # markers reported under any query string (e.g. a submitted GET form), matched by path only
    reported_by_path = {GT_BY_PATH[e["path_template"]] for e in endpoints if e["path_template"] in GT_BY_PATH}
    check(f"{name}: markers matched by key are a subset of markers matched by path", set(reported) <= reported_by_path)

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
        h["new_markers"] = sum(1 for mh, tool in first_reporter.items()
                               if tool == h["chosen_tool"] and first_iter[mh] == h["iteration"])

    # ---- consistency checks against the raw data
    recomputed = run_metrics(endpoints=endpoints, gt=GT, history=hist, events=events)
    stored = {k: v for k, v in m.items() if k != "server_markers_scored"}
    diff = [k for k in stored if stored[k] != recomputed.get(k)]
    check(f"{name}: metrics.json equals metrics recomputed from state.db + run_events.jsonl", not diff, str(diff))
    tp = set(GT_BY_HASH) & {e["canonical_hash"] for e in endpoints}
    check(f"{name}: markers reported = size of (endpoints ∩ ground truth) = markers in endpoint_sources",
          m["true_positives"] == len(tp) == len(reported), f"{m['true_positives']}, {len(tp)}, {len(reported)}")
    check(f"{name}: recall = reported / 59, precision = reported / discovered",
          m["recall"] == round(len(tp) / len(GT), 4) and m["discovered"] == len(endpoints)
          and m["precision"] == round(len(tp) / len(endpoints), 4))
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
    fc_windows = [(_ts(e["ts"]).timestamp() - e["tool_duration_seconds"], _ts(e["ts"]).timestamp())
                  for e in events if e["event"] == "tool_run" and e["chosen_tool"] == "firecrawl_navigate"]
    requested_by_fc = {h for h, t in first_hit.items() if any(a <= t.timestamp() <= b for a, b in fc_windows)}
    secs = round((_ts(finished["ts"]) - _ts(started["ts"])).total_seconds(), 1)
    check(f"{name}: run ended by saturation; time = run_finished − run_started",
          finished.get("termination") == "saturation" and secs == m["time_to_saturation_s"],
          f"{finished.get('termination')}, {secs}s")
    times = sorted((_ts(e["created_at"]) - _ts(started["ts"])).total_seconds() for e in endpoints
                   if e["canonical_hash"] in tp)
    t = lambda f: round(max(0.0, times[math.ceil(f * len(times)) - 1]), 1)
    check(f"{name}: t50 / t90 recomputed from endpoints.created_at", (m["t50_s"], m["t90_s"]) == (t(0.5), t(0.9)),
          f"{(m['t50_s'], m['t90_s'])} vs {(t(0.5), t(0.9))}")
    check(f"{name}: all tool runs ended ok", all(h["status"] == "ok" for h in hist))
    return {"name": name, "metrics": m, "history": hist, "reported": reported, "first_reporter": first_reporter,
            "requested": requested, "tool_endpoints": tool_endpoints, "termination": finished.get("termination"),
            "fc_jobs": sum(n for n, _ in jobs),
            "requested_by_fc": requested_by_fc, "reported_by_path": reported_by_path}


# ---------------------------------------------------------------------------- load everything
data = {}
for config, labels in CONFIGS:
    runs = []
    for label in labels:
        for d in sorted((ROOT / label).glob("run-*")):
            if (d / "metrics.json").exists():
                runs.append(load(d, label))
    data[config] = runs
    check(f"{config}: 3 complete runs", len(runs) == 3, str([r["name"] for r in runs]))

out = []
w = out.append
w("# Crawlground series: ZAP + Wapiti vs Firecrawl only vs full pipeline\n")
w("Benchmark: Crawlground (`zaproxy/crawlground@7c7ba59`), ground truth = 59 markers "
  "(`evaluation/ground_truth/crawlground.json`). Runs on 2026-10-16, 3 per configuration, each with a "
  "restarted Crawlground container, a restarted ZAP container and a fresh Crawlground tool name.\n")
w("| Configuration | Run folders | Settings |\n|---|---|---|")
w("| ZAP + Wapiti | `zap-wapiti/run-01..03` | `tools.firecrawl.enabled=false`, `tools.http_probe.enabled=false` |")
w("| Firecrawl only | `firecrawl-only/run-01..03` | ZAP, Wapiti, probe disabled; `orchestrator.stages=[[\"firecrawl_navigate\"]]` (seed: base URL only) |")
w("| Full pipeline | `full-pipeline/run-01`, `full-pipeline-b/run-01..02` | default `configs/app_config.json` |\n")
w("Deviations from the protocol in `docs/benchmarks.md`: 3 runs instead of ≥ 5, and configurations were run "
  "back to back instead of alternating. The full pipeline was paused after its first run to switch the Firecrawl "
  "API key; the interrupted second attempt was discarded and the remaining two runs were made under the label "
  "`full-pipeline-b`.\n")

# ---------------------------------------------------------------------------- definitions
w("## Metric definitions\n")
w("| Metric | Definition | Source |\n|---|---|---|")
for row in [
    ("Markers reported", "Ground-truth markers that at least one tool reported in its results (the orchestrator's record)", "`state.db`: endpoints ∩ ground truth"),
    ("Markers requested (server)", "Markers whose URL Crawlground's server received a request for under this run's tool name (the server's record)", "`server_results.json`"),
    ("Recall", "Markers reported / 59", "`metrics.json`"),
    ("Discovered endpoints", "Distinct normalized endpoints in the run's store (markers, test pages, scripts, forms, query variants, plus the initial seed)", "`state.db`: endpoints"),
    ("Precision", "Markers reported / discovered endpoints. Low by construction: the ground truth holds only the 59 markers, so every legitimate test page counts against it", "`metrics.json`"),
    ("Unique valid endpoints", "Discovered endpoints with a known status 2xx/3xx/401/403. Firecrawl reports no status codes, so its endpoints never count", "`metrics.json`"),
    ("Duplicate rate", "1 − discovered / raw results of all tool runs (how much tool output was redundant)", "`metrics.json`"),
    ("Time to saturation", "`run_started` → `run_finished`; the run ends when no rule fires", "`run_events.jsonl`"),
    ("t50 / t90", "Time until 50% / 90% of the run's own final marker count had been stored (`endpoints.created_at`). Results are stored when a tool run finishes, so a one-job run has t50 = t90 = its run time", "`state.db`, `run_events.jsonl`"),
    ("Requests", "Σ `requests_sent` over the tool runs. Firecrawl reports none and is stored as 0 (see below)", "`state.db`: execution_history"),
    ("Tool runs / failed / timed out", "Rows in execution_history and those with status `failed` / `timeout`", "`state.db`: execution_history"),
    ("Firecrawl jobs", "Agent jobs started (`[FIRECRAWL] Job k/N`), each with up to 10 seed URLs", "`orchestrator.log`"),
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

# ---------------------------------------------------------------------------- summary
w("## Summary over the runs\n")
w("ZAP + Wapiti and the full pipeline: mean ± sample SD [95% t-interval, n = 3]. A zero SD means the runs were "
  "identical, and the interval collapses to one value. **Firecrawl only: median [min–max]**, because its spread is so "
  "large that a t-interval with n = 3 is meaningless (for recall it would be "
  f"[{aggregate([r['metrics'] for r in data['Firecrawl only']])['recall']['ci95'][0]}, "
  f"{aggregate([r['metrics'] for r in data['Firecrawl only']])['recall']['ci95'][1]}], outside [0, 1]). "
  "Per-run values are in the next table.\n")
w("| Metric | " + " | ".join(f"{c} (n = {len(data[c])})" for c, _ in CONFIGS) + " |")
w("|---|" + "---|" * len(CONFIGS))
aggs = {c: aggregate([r["metrics"] for r in data[c]]) for c, _ in CONFIGS}
for c, _ in CONFIGS:
    for key, _ in SUMMARY_KEYS:
        vals = [r["metrics"][key] for r in data[c]]
        a = aggs[c][key]
        check(f"{c}: {key} mean/SD equal statistics.mean/stdev",
              a["mean"] == round(statistics.mean(vals), 4) and a["sd"] == round(statistics.stdev(vals), 4))
for key, label in SUMMARY_KEYS:
    cells = []
    for c, _ in CONFIGS:
        vals = [r["metrics"][key] for r in data[c]]
        if c in ROBUST:
            cells.append(f"{num(statistics.median(vals), key)} [{num(min(vals), key)}–{num(max(vals), key)}]")
        else:
            a = aggs[c][key]
            cells.append(f"{num(a['mean'], key)} ± {num(a['sd'], key, sd=True) if a['sd'] else '0'} "
                         f"[{num(a['ci95'][0], key)}, {num(a['ci95'][1], key)}]")
    w(f"| {label} | " + " | ".join(cells) + " |")
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
  f"{fo_hard_reported} reports of the {len(hard)} hard markers (over the three runs), only {fo_hard_both} came with a "
  "request for that marker. So Firecrawl most likely gets these URLs by reading the page source rather than by triggering "
  "the hover, focus, scroll or frame behaviour; in the pipeline, other tools then request what it reports.")
w(f"4. **Cost:** the full pipeline takes {t_fp / t_zw:.1f}× as long as ZAP + Wapiti "
  f"({t_fp / 60:.1f} vs {t_zw / 60:.1f} min on average). The Firecrawl steps account for "
  f"{', '.join(f'{x:.1f}' for x in fp_fc_min)} min per run; Firecrawl's requests are not measured.\n")


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
diffs = [(c, r) for c, _ in CONFIGS for r in data[c] if len(r["reported_by_path"]) != len(r["reported"])]
w("3. **Markers reported only with a query string are not counted.** A submitted GET form is reported as e.g. "
  "`/score/forms/04-select-field?colour=green`, which normalizes to a different key than the marker. Runs where "
  "matching by path alone gives a higher count: "
  + ("; ".join(f"`{r['name']}` {len(r['reported'])} → {len(r['reported_by_path'])}" for c, r in diffs) or "none")
  + ". Firecrawl-only recall by path: "
  + ", ".join(f"{len(r['reported_by_path']) / len(GT):.4f}" for r in fo)
  + f" (median {statistics.median(len(r['reported_by_path']) / len(GT) for r in fo):.4f}). The tables above use the "
  "stored metric (match by key).")
w("4. **Firecrawl's own traffic is not observed.** Its requests are not counted (only Crawlground's marker hits are "
  "visible), and the client keeps only the `links` of each job; the `actions_taken` list Firecrawl returns is "
  "discarded. Which pages Firecrawl actually loaded, and how it found each URL, is unknown.\n")
w("Firecrawl only, matched by path (Firecrawl is the only tool in these runs, so every request the server saw was "
  "Firecrawl's):\n")
w("| Run | Reported | Requested | Both | Reported, not requested | …of which URL is in the page source "
  "| Requested, not reported |")
w("|---|---|---|---|---|---|---|")
for r in fo:
    rep, req = r["reported_by_path"], r["requested"]
    w(f"| `{r['name']}` | {len(rep)} | {len(req)} | {len(rep & req)} | {len(rep - req)} "
      f"| {sum(map(in_src, rep - req))} | {len(req - rep)}" + (": " + ", ".join(f"`{marker(h)}`" for h in sorted(req - rep, key=marker)) if req - rep else "") + " |")
w("")

# ---------------------------------------------------------------------------- per run
w("## Per run\n")
w("| Configuration | Run | Markers reported | Requested (server) | Recall | Discovered | Precision | Unique valid "
  "| Duplicate rate | Time (min) | t50 (min) | t90 (min) | Requests | Tool runs | Firecrawl jobs | Ended by |")
w("|---|---|" + "---|" * 14)
for c, _ in CONFIGS:
    for r in data[c]:
        m = r["metrics"]
        w(f"| {c} | `{r['name']}` | {m['true_positives']} | {m['server_markers_scored']} | {num(m['recall'], 'recall')} "
          f"| {m['discovered']} | {num(m['precision'], 'precision')} | {m['unique_valid']} | {num(m['duplicate_rate'], 'duplicate_rate')} "
          f"| {num(m['time_to_saturation_s'], 'time_to_saturation_s')} | {num(m['t50_s'], 't50_s')} "
          f"| {num(m['t90_s'], 't90_s')} | {m['requests']:,} | {m['tool_runs']} | {r['fc_jobs']} | {r['termination']} |")
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

# ---------------------------------------------------------------------------- ZAP + Wapiti per tool
w("## ZAP + Wapiti, per tool\n")
w("Totals over each tool's runs within one crawl. \"Distinct endpoints\" and \"markers\" count everything the tool "
  "reported, including what another tool had already found; \"first found\" counts markers this tool stored first.\n")
w("| Run | Tool | Tool runs | Seeds | Requests | Raw results | New endpoints | Distinct endpoints reported "
  "| Markers reported | Markers first found | Minutes |")
w("|---|---|---|---|---|---|---|---|---|---|---|")
for r in data["ZAP + Wapiti"]:
    totals = Counter()
    for tool in ("zap_traditional_spider", "zap_client_spider", "wapiti_form_crawl"):
        hs = [h for h in r["history"] if h["chosen_tool"] == tool]
        markers = sum(1 for tools in r["reported"].values() if tool in tools)
        first = sum(1 for t in r["first_reporter"].values() if t == tool)
        row = {"runs": len(hs), "seeds": sum(h["seeds_count"] for h in hs),
               "req": sum(h["requests_sent"] for h in hs), "raw": sum(h["raw_results"] for h in hs),
               "new": sum(h["new_endpoints_discovered"] for h in hs), "first": first,
               "min": sum(h["tool_duration_seconds"] for h in hs) / 60}
        totals.update(row)
        w(f"| `{r['name']}` | {TOOL_LABEL[tool]} | {row['runs']} ({' + '.join(h['rule_id'] for h in hs)}) "
          f"| {' + '.join(str(h['seeds_count']) for h in hs)} | {row['req']:,} | {row['raw']:,} | {row['new']} "
          f"| {len(r['tool_endpoints'][tool])} | {markers} | {first} | {row['min']:.1f} |")
    m = r["metrics"]
    check(f"{r['name']}: per-tool totals add up to the run's requests and new endpoints",
          totals["req"] == m["requests"] and totals["new"] + 1 == m["discovered"]
          and totals["first"] == m["true_positives"])
    w(f"| `{r['name']}` | **All tools** | {totals['runs']} | | {totals['req']:,} | {totals['raw']:,} "
      f"| {totals['new']} (+1 initial seed) | {m['discovered']} | {m['true_positives']} | {totals['first']} "
      f"| run time {num(m['time_to_saturation_s'], 'time_to_saturation_s')} |")
w("")

# ---------------------------------------------------------------------------- categories
w("## Markers reported per category (run 1 / run 2 / run 3)\n")
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
w("| Marker | Firecrawl only: reported | Firecrawl only: requested | Full pipeline: reported "
  "| Full pipeline: first reported by | Full pipeline: first requested during a Firecrawl run |")
w("|---|---|---|---|---|---|")
for h in never:
    fo, fp = data["Firecrawl only"], data["Full pipeline"]
    firsts = Counter(TOOL_LABEL[r["first_reporter"][h]] for r in fp if h in r["first_reporter"])
    w(f"| `{marker(h)}` | {sum(h in r['reported'] for r in fo)}/{len(fo)} | {sum(h in r['requested'] for r in fo)}/{len(fo)} "
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
