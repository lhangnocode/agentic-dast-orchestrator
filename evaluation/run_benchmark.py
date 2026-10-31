"""
Benchmark runner: repeated, isolated crawls of one benchmark target + metrics.

  python -m evaluation.run_benchmark --benchmark juice-shop --runs 5 --label client-spider
  python -m evaluation.run_benchmark --benchmark juice-shop --runs 5 --label ajax \\
         --set orchestrator.rules.browser_spider=zap_ajax_spider \\
         --set orchestrator.stages='[["zap_traditional_spider","zap_ajax_spider","wapiti_form_crawl"],["firecrawl_navigate"]]'

Per run: the target container is restarted (fresh application state), ZAP gets a new empty session,
Crawlground gets a new tool name (fresh server-side scores), and the orchestrator writes everything
(state.db, run_events.jsonl, logs, prompts, endpoints) to output/benchmarks/<benchmark>/<label>/run-NN/.
Metrics per run go to metrics.json; summary.json / summary.md hold mean ± 95% CI over the runs.
Run configurations to compare (e.g. Client vs AJAX Spider) as separate labels, alternating their order.

With --tunnel-proxy PORT, a logging reverse proxy (tools/traffic_proxy.py) listens on PORT during each run
and forwards to the target; point the tunnel at it (cloudflared tunnel --url http://localhost:PORT).
Only Firecrawl uses the tunnel, so run-NN/tunnel_traffic.jsonl lists exactly Firecrawl's requests.

A profile with "access_log_container" (e.g. Crawl Maze's Flask server) has that container's access log saved
to run-NN/target_access.log after each run; server_resources_requested counts the ground-truth paths the
target itself received (any method or query), independent of what the tools reported.
Each run also keeps its resolved config (config.json) and one trace file per tool run (tool_runs/).

  python -m evaluation.run_benchmark --benchmark crawlground --label zap-wapiti --recompute

rescores the label's existing runs against the current ground truth (no crawling) and rewrites
metrics.json / summary.*; server-side values (server_markers_scored) are kept from the old metrics.
"""

import argparse
import copy
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from evaluation.crawl_metrics import aggregate, load_ground_truth, load_run, run_metrics
from tools.traffic_proxy import TrafficProxy, read_traffic

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "benchmarks" / "docker-compose.yml"
ZAP_COMPOSE_FILE = ROOT / "docker-compose.yml"
SUMMARY_KEYS = ["recall", "recall_client_route", "recall_server_endpoint", "recall_marker",
                "recall_expected_resource", "recall_reviewed", "precision", "true_positives", "discovered",
                "spa_routes_discovered", "unique_valid", "duplicate_rate", "time_to_saturation_s", "t50_s",
                "t90_s", "requests", "tool_runs", "failed_runs", "timeout_runs", "server_markers_scored",
                "firecrawl_requests_observed", "firecrawl_paths_observed", "startup_overhead_s", "crawl_time_s",
                "startup_share", "zap_restart_s", "server_resources_requested", "target_requests_logged"]

logger = logging.getLogger("benchmark")


def deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def apply_override(config: Dict[str, Any], assignment: str) -> None:
    """'a.b.c=value' with value parsed as JSON when possible (numbers, null, lists, ...)."""
    path, _, raw = assignment.partition("=")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    node = config
    keys = path.split(".")
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def run_config(base: Dict[str, Any], profile: Dict[str, Any], overrides: List[str], run_dir: Path) -> Dict[str, Any]:
    config = deep_merge(base, profile.get("config", {}))
    for assignment in overrides:
        apply_override(config, assignment)
    rd = str(run_dir)
    config.setdefault("storage", {})["sqlite_db"] = f"{rd}/state.db"
    config.setdefault("logging", {})["run_events_file"] = f"{rd}/run_events.jsonl"
    config["logging"]["tool_runs_dir"] = f"{rd}/tool_runs"
    config.setdefault("output", {})["dir"] = rd
    w = config.setdefault("tools", {}).setdefault("wapiti", {})
    w.update(session_file=f"{rd}/wapiti/session.db", report_file=f"{rd}/wapiti/report.json",
             log_file=f"{rd}/wapiti.log")
    config["tools"].setdefault("firecrawl", {}).update(prompts_dir=f"{rd}/prompts",
                                                       responses_dir=f"{rd}/firecrawl_responses")
    return config


def wait_until_up(url: str, timeout_s: int = 120) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=3).status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def restart_zap(zap, timeout_s: int = 180) -> None:
    """
    Restarts the ZAP container and waits until its API answers. Unlike a new ZAP session,
    a restart is guaranteed to drop all add-on state (e.g. the Client Spider's Client Map),
    so nothing discovered in an earlier run can leak into the next one.
    """
    subprocess.run(["docker", "compose", "-f", str(ZAP_COMPOSE_FILE), "restart", "zap"], check=True,
                   capture_output=True, text=True)
    zap._client = None  # reconnect to the restarted daemon
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if zap.is_alive():
            return
        time.sleep(3)
    raise RuntimeError("ZAP did not come back after restart")


def reset_target(profile: Dict[str, Any], base_url: str) -> None:
    service = profile.get("service")
    if service:
        subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "restart", service], check=True,
                       capture_output=True, text=True)
    if not wait_until_up(base_url):
        raise RuntimeError(f"benchmark target {base_url} did not come up")


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="Repeated, isolated crawl runs of one benchmark + metrics.")
    parser.add_argument("--benchmark", required=True, help="profile name in benchmarks/<name>.json")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--label", default="default", help="name of the configuration being measured")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="config override, e.g. orchestrator.rules.browser_spider=zap_ajax_spider")
    parser.add_argument("--config", default=str(ROOT / "configs" / "app_config.json"))
    parser.add_argument("--no-reset", action="store_true", help="do not restart the target between runs")
    parser.add_argument("--no-zap-restart", action="store_true",
                        help="only start a new ZAP session instead of restarting the ZAP container (faster, "
                             "but add-on state such as the Client Map may carry over between runs)")
    parser.add_argument("--tunnel-proxy", type=int, metavar="PORT",
                        help="log Firecrawl's requests: run a logging proxy on PORT -> target for each run "
                             "(the public tunnel must point at http://localhost:PORT)")
    parser.add_argument("--recompute", action="store_true",
                        help="rescore the label's existing runs against the current ground truth, no crawling")
    args = parser.parse_args(argv)
    if args.recompute:
        return recompute(args.benchmark, args.label)

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    profile = json.loads((ROOT / "benchmarks" / f"{args.benchmark}.json").read_text())
    base = json.loads(Path(args.config).read_text())
    gt = load_ground_truth(str(ROOT / profile["ground_truth"]))
    out_dir = ROOT / "output" / "benchmarks" / args.benchmark / args.label
    out_dir.mkdir(parents=True, exist_ok=True)

    from orchestrator.loop import AdaptiveOrchestrator   # imported after .env is loaded
    from tools.registry import ToolRegistry

    # A label is one configuration: new runs are appended (run numbers continue), never overwritten
    prev_file = out_dir / "summary.json"
    if prev_file.exists():
        prev = json.loads(prev_file.read_text())
        if prev.get("overrides") != args.set:
            print(f"label {args.label!r} was run with other overrides ({prev.get('overrides')}); use another label")
            return 2
    existing = [int(d.name[4:]) for d in out_dir.glob("run-[0-9][0-9]")]
    first = max(existing, default=0) + 1

    for i in range(first, first + args.runs):
        run_dir = out_dir / f"run-{i:02d}"
        run_dir.mkdir(parents=True, exist_ok=False)
        config = run_config(base, profile, args.set, run_dir)
        if args.tunnel_proxy:
            config["tools"].setdefault("firecrawl", {})["traffic_log"] = str(run_dir / TRAFFIC_FILE)
        base_url = config["target"]["base_url"]
        logger.info(f"=== {args.benchmark} / {args.label}: run {i} ({i - first + 1}/{args.runs}) -> {run_dir} ===")
        if not args.no_reset:
            reset_target(profile, base_url)
        (run_dir / "config.json").write_text(json.dumps(config, indent=2))
        log_since = time.time()

        tool_name = f"{args.label}-run{i:02d}-{int(time.time())}"
        if profile.get("crawlground_scores"):
            requests.post(f"{base_url.rstrip('/')}/set-tool", json={"name": tool_name}, timeout=10)

        tools = ToolRegistry.from_config(config)
        t0 = time.time()
        if args.no_zap_restart:
            tools.zap.new_session()
        else:
            logger.info("Restarting ZAP for a clean state ...")
            restart_zap(tools.zap)
        meta = {"overrides": args.set, "zap_restart_per_run": not args.no_zap_restart,
                "zap_restart_s": round(time.time() - t0, 1), "tunnel_proxy": args.tunnel_proxy,
                "crawlground_tool_name": tool_name if profile.get("crawlground_scores") else None,
                "zap_addons": zap_addon_versions(tools.zap)}
        (run_dir / "run_meta.json").write_text(json.dumps(meta, indent=2))
        file_handler = logging.FileHandler(run_dir / "orchestrator.log", encoding="utf-8")
        logging.getLogger().addHandler(file_handler)
        proxy = (TrafficProxy(args.tunnel_proxy, base_url, str(run_dir / TRAFFIC_FILE)).start()
                 if args.tunnel_proxy else None)
        try:
            AdaptiveOrchestrator(config, tools=tools).run()
        finally:
            if proxy:
                proxy.stop()
            logging.getLogger().removeHandler(file_handler)

        if profile.get("access_log_container"):
            save_access_log(profile["access_log_container"], log_since, run_dir)
        metrics = run_metrics(gt=gt, **load_run(str(run_dir)))
        metrics.update(traffic_metrics(run_dir))
        metrics.update(access_log_metrics(run_dir, gt))
        metrics["zap_restart_s"] = meta["zap_restart_s"]
        if profile.get("crawlground_scores"):
            report = requests.get(f"{base_url.rstrip('/')}/results.json", timeout=10).json()
            (run_dir / "server_results.json").write_text(json.dumps(report, indent=2))
            mine = next((t for t in report["summary"]["tools"] if t["name"] == tool_name), None)
            metrics["server_markers_scored"] = mine["scored"] if mine else 0
        (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
        logger.info(f"[RUN {i}] recall={metrics['recall']} precision={metrics['precision']} "
                    f"tp={metrics['true_positives']}/{metrics['ground_truth']} "
                    f"time={metrics.get('time_to_saturation_s')}s startup={metrics.get('startup_overhead_s')}s")

    # Summary over every run of the label, old and new
    all_metrics = [json.loads((d / "metrics.json").read_text())
                   for d in sorted(out_dir.glob("run-[0-9][0-9]")) if (d / "metrics.json").exists()]
    summary = {"benchmark": args.benchmark, "label": args.label, "runs": len(all_metrics), "overrides": args.set,
               "zap_restart_per_run": not args.no_zap_restart,
               "ground_truth": profile["ground_truth"], "metrics": aggregate(all_metrics)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "summary.md").write_text(render_summary(summary))
    print(render_summary(summary))
    return 0


SERVER_KEYS = ("server_markers_scored", "zap_restart_s")   # measured during the run, kept by --recompute


def zap_addon_versions(zap) -> Dict[str, str]:
    """Versions of the ZAP add-ons the crawl depends on (ZAP auto-updates add-ons when a container is created)."""
    try:
        return {a["id"]: a["version"] for a in zap.client.autoupdate.installed_addons
                if a.get("id") in ("client", "spider", "spiderAjax", "authhelper", "commonlib", "network", "selenium")}
    except Exception as e:
        return {"error": str(e)}
TRAFFIC_FILE = "tunnel_traffic.jsonl"


def traffic_metrics(run_dir: Path) -> Dict[str, Any]:
    """Requests seen on the tunnel (= Firecrawl's), when the run had a logging proxy."""
    if not (run_dir / TRAFFIC_FILE).exists():
        return {}
    traffic = read_traffic(str(run_dir / TRAFFIC_FILE))
    return {"firecrawl_requests_observed": len(traffic),
            "firecrawl_paths_observed": len({t["path"].split("?", 1)[0] for t in traffic})}


ACCESS_LOG_FILE = "target_access.log"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ACCESS_LINE = re.compile(r'"([A-Z]+) (\S+) HTTP/[\d.]+" (\d{3})')


def save_access_log(container: str, since: float, run_dir: Path) -> None:
    """The target container's log since the run started (the Flask dev server logs one line per request)."""
    out = subprocess.run(["docker", "logs", "--since", f"{since:.3f}", container],
                         capture_output=True, text=True)
    (run_dir / ACCESS_LOG_FILE).write_text(out.stdout + out.stderr, encoding="utf-8")


def access_log_metrics(run_dir: Path, gt: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Ground-truth paths the target received (any method/query), from its saved access log."""
    log = run_dir / ACCESS_LOG_FILE
    if not log.exists():
        return {}
    text = ANSI.sub("", log.read_text(encoding="utf-8"))   # Flask colours non-200 status lines
    requested = [m.group(2).split("?", 1)[0] for m in ACCESS_LINE.finditer(text)]
    gt_paths = {g["path_template"].split("?", 1)[0] for g in gt}
    return {"target_requests_logged": len(requested),
            "server_resources_requested": len(gt_paths & set(requested))}


def recompute(benchmark: str, label: str) -> int:
    """Rewrites metrics.json of every run-NN in the label, and its summary, from the stored run data."""
    profile = json.loads((ROOT / "benchmarks" / f"{benchmark}.json").read_text())
    gt = load_ground_truth(str(ROOT / profile["ground_truth"]))
    out_dir = ROOT / "output" / "benchmarks" / benchmark / label
    run_dirs = sorted(d for d in out_dir.glob("run-[0-9][0-9]") if (d / "state.db").exists())
    if not run_dirs:
        print(f"no runs with a state.db in {out_dir}")
        return 1
    all_metrics = []
    for run_dir in run_dirs:
        old_file = run_dir / "metrics.json"
        old = json.loads(old_file.read_text()) if old_file.exists() else {}
        metrics = run_metrics(gt=gt, **load_run(str(run_dir)))
        metrics.update({k: old[k] for k in SERVER_KEYS if k in old})
        metrics.update(traffic_metrics(run_dir))
        metrics.update(access_log_metrics(run_dir, gt))
        old_file.write_text(json.dumps(metrics, indent=2))
        all_metrics.append(metrics)
        print(f"{run_dir.name}: recall {old.get('recall')} -> {metrics['recall']}, "
              f"precision {old.get('precision')} -> {metrics['precision']}")
    summary_file = out_dir / "summary.json"
    summary = json.loads(summary_file.read_text()) if summary_file.exists() else {
        "benchmark": benchmark, "label": label, "overrides": [], "ground_truth": profile["ground_truth"]}
    summary.update(runs=len(run_dirs), metrics=aggregate(all_metrics))
    summary_file.write_text(json.dumps(summary, indent=2))
    (out_dir / "summary.md").write_text(render_summary(summary))
    return 0


def render_summary(summary: Dict[str, Any]) -> str:
    lines = [f"# {summary['benchmark']} / {summary['label']} ({summary['runs']} runs)", "",
             f"Overrides: {', '.join(summary['overrides']) or 'none'}", "",
             f"ZAP restarted before each run: {'yes' if summary.get('zap_restart_per_run') else 'no'}", "",
             "| Metric | Mean | SD | 95% CI |", "| --- | ---: | ---: | --- |"]
    for key in SUMMARY_KEYS:
        s = summary["metrics"].get(key)
        if s:
            lines.append(f"| {key} | {s['mean']} | {s['sd']} | [{s['ci95'][0]}, {s['ci95'][1]}] |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
