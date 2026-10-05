#!/usr/bin/env python3
"""
CLI entrypoint for Research MVP: Adaptive Web Crawling and Endpoint Discovery for DAST.
Supports Adaptive Orchestration and Standalone Baseline Scans.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from dotenv import load_dotenv

from normalization.normalizer import normalize_endpoint
from orchestrator.loop import AdaptiveOrchestrator
from tools.firecrawl_client import FirecrawlClient
from tools.wapiti_wrapper import WapitiWrapper
from tools.zap_wrapper import ZapWrapper


def setup_logging(log_file: str = "output/logs/orchestrator.log", log_level: str = "INFO"):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def run_standalone_zap(config: dict) -> dict:
    logging.info("=== Running Standalone ZAP Baseline ===")
    zap_cfg = config.get("tools", {}).get("zap", {})
    base_url = config.get("target", {}).get("base_url", "http://localhost/")
    zap = ZapWrapper(
        api_url=zap_cfg.get("api_url", "http://127.0.0.1:18080"),
        api_key=zap_cfg.get("api_key", "changeme")
    )

    zap.run_traditional_spider([base_url])
    zap.run_ajax_spider([base_url])
    raw_msgs = zap.collect_messages()

    normalized = []
    for m in raw_msgs:
        norm = normalize_endpoint(m["url"], method=m["method"], source_tool="zap_standalone", status_code=m.get("status_code", 200))
        if norm:
            normalized.append(norm)

    return _export_baseline_results(normalized, "zap_standalone")


def run_standalone_wapiti(config: dict) -> dict:
    logging.info("=== Running Standalone Wapiti Baseline ===")
    wapiti_cfg = config.get("tools", {}).get("wapiti", {})
    base_url = config.get("target", {}).get("base_url", "http://localhost/")
    wapiti = WapitiWrapper(
        binary_path=wapiti_cfg.get("binary", "wapiti"),
        scope=wapiti_cfg.get("scope", "domain"),
        session_db_path="output/temp/wapiti/standalone_session.db",
        report_path="output/temp/wapiti/standalone_report.json"
    )

    raw_eps = wapiti.run_crawl_only(base_url=base_url)
    normalized = []
    for m in raw_eps:
        norm = normalize_endpoint(m["url"], method=m["method"], source_tool="wapiti_standalone", status_code=m.get("status_code", 200))
        if norm:
            normalized.append(norm)

    return _export_baseline_results(normalized, "wapiti_standalone")


def run_standalone_firecrawl(config: dict) -> dict:
    logging.info("=== Running Standalone Firecrawl Baseline ===")
    firecrawl_cfg = config.get("tools", {}).get("firecrawl", {})
    base_url = config.get("target", {}).get("base_url", "http://localhost/")
    firecrawl = FirecrawlClient(
        api_url=firecrawl_cfg.get("api_url", "https://api.firecrawl.dev/v2"),
        model=firecrawl_cfg.get("model", "spark-2"),
        mock_fallback=True
    )

    raw_eps = firecrawl.run_agent_discovery(target_url=base_url)
    normalized = []
    for m in raw_eps:
        norm = normalize_endpoint(m["url"], method=m["method"], source_tool="firecrawl_standalone", status_code=m.get("status_code", 200))
        if norm:
            normalized.append(norm)

    return _export_baseline_results(normalized, "firecrawl_standalone")


def _export_baseline_results(normalized_endpoints: list, baseline_name: str) -> dict:
    output_dir = Path("output") / "experiments" / baseline_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Deduplicate by canonical hash
    unique_eps = {ep["canonical_hash"]: ep for ep in normalized_endpoints}.values()
    sorted_eps = sorted(list(unique_eps), key=lambda x: x["canonical_key"])

    txt_file = output_dir / "discovered_urls.txt"
    json_file = output_dir / "endpoints.json"

    with open(txt_file, "w", encoding="utf-8") as f:
        for ep in sorted_eps:
            f.write(f"{ep['normalized_url']}\n")

    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(sorted_eps, f, indent=2)

    logging.info(f"[{baseline_name.upper()}] Discovered {len(sorted_eps)} unique canonical endpoints -> {txt_file}")
    return {
        "baseline": baseline_name,
        "total_canonical_endpoints": len(sorted_eps),
        "output_txt": str(txt_file),
        "output_json": str(json_file)
    }


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Adaptive Web Crawling and Endpoint Discovery for DAST")
    parser.add_argument(
        "--mode",
        choices=["adaptive", "zap-only", "wapiti-only", "firecrawl-only"],
        default="adaptive",
        help="Execution mode (default: adaptive)"
    )
    parser.add_argument(
        "--config",
        default="configs/app_config.json",
        help="Path to JSON configuration file (default: configs/app_config.json)"
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=None,
        help="Override maximum orchestration iterations"
    )

    args = parser.parse_args()

    # Load configuration
    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"Error: Config file not found at {cfg_path}")
        sys.exit(1)

    with open(cfg_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if args.max_iter:
        config.setdefault("orchestrator", {})["max_iterations"] = args.max_iter

    log_cfg = config.get("logging", {})
    setup_logging(
        log_file=log_cfg.get("log_file", "output/logs/orchestrator.log"),
        log_level=log_cfg.get("log_level", "INFO")
    )

    logging.info(f"Configuration loaded from {cfg_path}. Mode: {args.mode}")

    if args.mode == "adaptive":
        orchestrator = AdaptiveOrchestrator(config)
        summary = orchestrator.run()
        print("\n" + "=" * 50)
        print("ADAPTIVE ORCHESTRATION SUMMARY:")
        print(json.dumps(summary, indent=2))
        print("=" * 50 + "\n")

    elif args.mode == "zap-only":
        run_standalone_zap(config)

    elif args.mode == "wapiti-only":
        run_standalone_wapiti(config)

    elif args.mode == "firecrawl-only":
        run_standalone_firecrawl(config)


if __name__ == "__main__":
    main()
