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
"""

import argparse
import copy
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from evaluation.crawl_metrics import aggregate, load_ground_truth, load_run, run_metrics

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "benchmarks" / "docker-compose.yml"
ZAP_COMPOSE_FILE = ROOT / "docker-compose.yml"
SUMMARY_KEYS = ["recall", "recall_client_route", "recall_server_endpoint", "recall_marker",
                "recall_expected_resource", "recall_reviewed", "precision", "true_positives", "discovered",
                "spa_routes_discovered", "unique_valid", "duplicate_rate", "time_to_saturation_s", "t50_s",
                "t90_s", "requests", "tool_runs", "failed_runs", "timeout_runs", "server_markers_scored"]

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
    config.setdefault("output", {})["dir"] = rd
    w = config.setdefault("tools", {}).setdefault("wapiti", {})
    w.update(session_file=f"{rd}/wapiti/session.db", report_file=f"{rd}/wapiti/report.json",
             log_file=f"{rd}/wapiti.log")
    config["tools"].setdefault("firecrawl", {})["prompts_dir"] = f"{rd}/prompts"
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
    args = parser.parse_args(argv)

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

    all_metrics = []
    for i in range(1, args.runs + 1):
        run_dir = out_dir / f"run-{i:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        config = run_config(base, profile, args.set, run_dir)
        base_url = config["target"]["base_url"]
        logger.info(f"=== {args.benchmark} / {args.label}: run {i}/{args.runs} -> {run_dir} ===")
        if not args.no_reset:
            reset_target(profile, base_url)

        tool_name = f"{args.label}-run{i:02d}-{int(time.time())}"
        if profile.get("crawlground_scores"):
            requests.post(f"{base_url.rstrip('/')}/set-tool", json={"name": tool_name}, timeout=10)

        tools = ToolRegistry.from_config(config)
        if args.no_zap_restart:
            tools.zap.new_session()
        else:
            logger.info("Restarting ZAP for a clean state ...")
            restart_zap(tools.zap)
        file_handler = logging.FileHandler(run_dir / "orchestrator.log", encoding="utf-8")
        logging.getLogger().addHandler(file_handler)
        try:
            AdaptiveOrchestrator(config, tools=tools).run()
        finally:
            logging.getLogger().removeHandler(file_handler)

        metrics = run_metrics(gt=gt, **load_run(str(run_dir)))
        if profile.get("crawlground_scores"):
            report = requests.get(f"{base_url.rstrip('/')}/results.json", timeout=10).json()
            (run_dir / "server_results.json").write_text(json.dumps(report, indent=2))
            mine = next((t for t in report["summary"]["tools"] if t["name"] == tool_name), None)
            metrics["server_markers_scored"] = mine["scored"] if mine else 0
        (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
        all_metrics.append(metrics)
        logger.info(f"[RUN {i}] recall={metrics['recall']} precision={metrics['precision']} "
                    f"tp={metrics['true_positives']}/{metrics['ground_truth']} "
                    f"time={metrics.get('time_to_saturation_s')}s")

    summary = {"benchmark": args.benchmark, "label": args.label, "runs": args.runs, "overrides": args.set,
               "zap_restart_per_run": not args.no_zap_restart,
               "ground_truth": profile["ground_truth"], "metrics": aggregate(all_metrics)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "summary.md").write_text(render_summary(summary))
    print(render_summary(summary))
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
