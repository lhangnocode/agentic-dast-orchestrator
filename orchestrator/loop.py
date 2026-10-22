"""
Crawl orchestration loop (strategy 2: classic crawlers first, deterministic tool selection).

  Stages (fixed, `orchestrator.stages`), default:
    1. ZAP traditional spider -> ZAP Client Spider (one ZAP lane)  ||  Wapiti (own lane)
    2. Firecrawl, seeded with every page/route the first stage found
  Rule rounds (`orchestrator/rules.py`): each round a pure rule engine picks at most one
  action per engine from the observed state; engines run in parallel lanes. The crawl stops
  at saturation (no rule fires) or at a safety ceiling (`max_iterations`, `max_runs_per_tool`).

Every run is normalized into the SQLite store, and the coverage matrix is updated so a tool
is never re-seeded with pages it already visited. No LLM is involved in tool selection.
"""

import json
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from normalization.normalizer import NormalizationProfile, normalize_endpoint
from orchestrator.rules import (FIRECRAWL, WAPITI, ZAP_CLIENT, ZAP_TRADITIONAL, Action, RuleSettings,
                                build_snapshot, engine_of, select_actions, tool_frontier)
from orchestrator.run_log import RunLog
from orchestrator.safety_firewall import SafetyFirewall
from storage.state_store import StateStore
from tools.registry import ToolRegistry, ToolRun

logger = logging.getLogger("orchestrator.loop")

DEFAULT_STAGES = [[ZAP_TRADITIONAL, ZAP_CLIENT, WAPITI], [FIRECRAWL]]
LATER_STAGE_FOCUS = "pages, client-side views and workflows beyond what the crawlers already found"


class AdaptiveOrchestrator:
    def __init__(self, config: Dict[str, Any], tools: Optional[ToolRegistry] = None):
        self.config = config
        self.target_config = config.get("target", {})
        self.base_url = self.target_config.get("base_url", "http://localhost/")
        self.extra_seeds: List[str] = list(self.target_config.get("seed_urls", []))
        self.allowed_hosts = list(self.target_config.get("allowed_hosts", ["localhost", "127.0.0.1"]))

        self.tools = tools or ToolRegistry.from_config(config)
        tunnel_host = urlparse(self.tools.firecrawl.public_tunnel_url).hostname if self.tools.firecrawl.public_tunnel_url else None
        if tunnel_host and tunnel_host not in self.allowed_hosts:
            self.allowed_hosts.append(tunnel_host)
            logger.info(f"[INIT] Added public tunnel host to firewall allowed scope: {tunnel_host}")

        self.orch_config = config.get("orchestrator", {})
        self.max_iterations = self.orch_config.get("max_iterations", 30)
        self.stages: List[List[str]] = [list(st) for st in self.orch_config.get("stages", DEFAULT_STAGES)]
        self.parallel: bool = self.orch_config.get("parallel", True)
        self.rule_settings = RuleSettings.from_config(config)

        db_path = config.get("storage", {}).get("sqlite_db", "storage/orchestrator_state.db")
        self.store = StateStore(db_path=db_path)
        self.profile = NormalizationProfile.from_config(config)

        self.firewall = SafetyFirewall(allowed_hosts=self.allowed_hosts, base_url=self.base_url)
        # Firecrawl prompts include a summary of what is already known (spend effort on gaps)
        if hasattr(self.tools.firecrawl, "known_routes_provider"):
            self.tools.firecrawl.known_routes_provider = self.store.get_known_routes_summary
        self.run_log = RunLog(config.get("logging", {}).get("run_events_file", "output/logs/run_events.jsonl"))
        self.output_dir = Path(config.get("output", {}).get("dir", "output"))

    # ------------------------------------------------------------------ setup
    def seed_initial_target(self) -> None:
        """Seeds the base URL (and configured entry points) if the store is empty."""
        if self.store.get_total_endpoints_count() > 0:
            return
        for url in [self.base_url] + self.extra_seeds:
            ep = normalize_endpoint(url, method="GET", source_tool="initial_seed", status_code=0, profile=self.profile)
            if ep:
                self.store.insert_or_update_endpoint(ep, current_iteration=0)
        logger.info(f"[INIT] Seeded target: {self.base_url} (+{len(self.extra_seeds)} configured entry point(s))")

    # ------------------------------------------------------------------ main loop
    def run(self) -> Dict[str, Any]:
        logger.info(f"=== Starting DAST Crawl Orchestrator on {self.base_url} ===")
        self.seed_initial_target()

        available = self.tools.available_tools()
        logger.info(f"[INIT] Available tools: {available or 'none'} (run id {self.run_log.run_id})")
        self.run_log.event("run_started", base_url=self.base_url, available_tools=available, stages=self.stages,
                           parallel=self.parallel, max_iterations=self.max_iterations,
                           rules={"browser_spider": self.rule_settings.browser_spider,
                                  "max_retries": self.rule_settings.max_retries,
                                  "max_runs_per_tool": self.rule_settings.max_runs_per_tool,
                                  "disabled_rules": sorted(self.rule_settings.disabled_rules)})

        base_seeds = self.firewall.sanitize_seeds([self.base_url] + self.extra_seeds)
        rule_counts: Dict[str, int] = {}
        requests_sent = 0
        iteration = 0
        staged: set = set()

        # Fixed stages: stage 1 starts from the base URL, later stages from everything found so far
        for index, stage in enumerate(self.stages, start=1):
            tools = [t for t in stage if t in available]
            skipped = [t for t in stage if t not in available]
            if skipped:
                logger.warning(f"[STAGE {index}] Skipping unavailable tool(s): {skipped}")
            if not tools or iteration >= self.max_iterations:
                continue
            iteration += 1
            plan = [Action(t, self._stage_seeds(t, index, base_seeds), f"stage{index}",
                           f"fixed stage {index}", "" if index == 1 else LATER_STAGE_FOCUS) for t in tools]
            logger.info(f"\n--- [Iteration {iteration}] Stage {index}: {tools} ---")
            for run in self._run_plan(plan, iteration):
                requests_sent += run.requests_sent
                rule_counts[f"stage{index}"] = rule_counts.get(f"stage{index}", 0) + 1
            staged.update(tools)

        # Rule rounds until saturation or a safety ceiling
        termination = "saturation"
        while True:
            if iteration >= self.max_iterations:
                termination = "max_iterations"
                break
            snapshot = build_snapshot(self.store, available, self.rule_settings, base_seeds, staged, iteration + 1)
            actions = select_actions(snapshot, self.rule_settings)
            if not actions:
                break
            iteration += 1
            logger.info(f"\n--- [Iteration {iteration}] Rules: "
                        + ", ".join(f"{a.rule_id}->{a.tool}" for a in actions) + " ---")
            for a in actions:
                logger.info(f"[Decision:{a.rule_id}] {a.tool} - {a.reason}")
                self.run_log.event("decision", iteration=iteration, rule_id=a.rule_id, action=a.tool,
                                   seeds_count=len(a.seeds), reasoning=a.reason, surface=snapshot.surface)
            for run in self._run_plan(actions, iteration):
                requests_sent += run.requests_sent
            for a in actions:
                rule_counts[a.rule_id] = rule_counts.get(a.rule_id, 0) + 1

        logger.info(f"[STOP] {termination} after {iteration} iteration(s).")
        summary = self.export_results()
        summary["total_requests_sent"] = requests_sent
        summary["rule_counts"] = rule_counts
        summary["termination"] = termination
        summary["iterations_run"] = iteration
        summary["run_id"] = self.run_log.run_id
        self.run_log.event("run_finished", **{k: v for k, v in summary.items() if not k.startswith("output_")})
        return summary

    # ------------------------------------------------------------------ execution
    def _stage_seeds(self, tool: str, stage_index: int, base_seeds: List[str]) -> List[str]:
        if stage_index == 1:
            return list(base_seeds)
        frontier = [s for s in tool_frontier(self.store, tool) if self.firewall.is_in_scope(s)]
        return list(dict.fromkeys(base_seeds + frontier))

    def _run_plan(self, plan: List[Action], iteration: int) -> List[ToolRun]:
        """
        Runs the planned actions. Tools of the same engine run sequentially in one lane;
        lanes run in parallel if configured. Results are ingested in plan order afterwards.
        """
        lanes: "OrderedDict[str, List[Action]]" = OrderedDict()
        for action in plan:
            action.seeds = self.firewall.sanitize_seeds(action.seeds)
            lanes.setdefault(engine_of(action.tool), []).append(action)

        def run_lane(items: List[Action]) -> List[ToolRun]:
            return [self.tools.run(a.tool, a.seeds, self.base_url, focus=a.focus) for a in items]

        if self.parallel and len(lanes) > 1:
            with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
                lane_runs = list(pool.map(run_lane, lanes.values()))
        else:
            lane_runs = [run_lane(items) for items in lanes.values()]
        runs_by_tool = {run.tool: run for runs in lane_runs for run in runs}

        runs = []
        for action in plan:
            run = runs_by_tool[action.tool]
            before = self.store.get_total_endpoints_count()
            new = self._ingest(run, iteration)
            self._record(run, iteration, before, new, action.reason, rule_id=action.rule_id)
            runs.append(run)
        return runs

    # ------------------------------------------------------------------ state updates
    def _ingest(self, run: ToolRun, iteration: int) -> int:
        """Normalizes a tool run into the store and marks what the tool has now visited."""
        new = 0
        visited_hashes = []
        for item in run.results:
            norm = normalize_endpoint(
                item.get("url", ""),
                method=item.get("method", "GET"),
                source_tool=item.get("source", run.tool),
                status_code=item.get("status_code", 0),
                profile=self.profile,
            )
            if not norm:
                continue
            norm["content_type"] = item.get("content_type", "")
            if self.store.insert_or_update_endpoint(norm, current_iteration=iteration):
                new += 1
            visited_hashes.append(norm["canonical_hash"])

        for seed in run.seeds:
            norm = normalize_endpoint(seed, profile=self.profile)
            if norm:
                visited_hashes.append(norm["canonical_hash"])
        self.store.mark_hashes_processed(visited_hashes, run.tool, iteration)

        logger.info(
            f"[{run.tool}] {len(run.results)} raw result(s) -> {new} new canonical endpoint(s) | "
            f"requests: {run.requests_sent} | {run.duration_seconds:.1f}s | status: {run.status}"
            + (f" | error: {run.error}" if run.error else "")
        )
        return new

    def _record(self, run: ToolRun, iteration: int, endpoints_before: int, new: int, reasoning: str,
                rule_id: str) -> None:
        """Persists one tool run (history row + JSONL event), including the tool's frontier afterwards."""
        record = {
            "iteration": iteration,
            "chosen_tool": run.tool,
            "rule_id": rule_id,
            "reasoning": reasoning,
            "target_seeds": run.seeds,
            "seeds_count": len(run.seeds),
            "endpoints_before": endpoints_before,
            "endpoints_after": self.store.get_total_endpoints_count(),
            "new_endpoints_discovered": new,
            "marginal_gain": new / max(endpoints_before, 1),
            "tool_duration_seconds": run.duration_seconds,
            "requests_sent": run.requests_sent,
            "raw_results": len(run.results),
            "status": run.status,
            "error": run.error,
            "frontier_fingerprint": self.store.frontier_fingerprint(run.tool),
        }
        self.store.record_iteration(record)
        self.run_log.event("tool_run", **{k: v for k, v in record.items() if k not in ("target_seeds", "reasoning")})

    # ------------------------------------------------------------------ export
    def export_results(self) -> Dict[str, Any]:
        """Saves canonical discovered endpoints to standard output files."""
        endpoints = self.store.get_all_endpoints()
        output_dir = self.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        txt_file = output_dir / "normalized_discovered_urls.txt"
        json_file = output_dir / "normalized_endpoints.json"

        unique_urls = sorted(dict.fromkeys(ep["normalized_url"] for ep in endpoints))
        with open(txt_file, "w", encoding="utf-8") as f:
            for u in unique_urls:
                f.write(f"{u}\n")

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(endpoints, f, indent=2)

        summary = {
            "total_canonical_endpoints": len(endpoints),
            "unique_normalized_urls": len(unique_urls),
            "tool_provenance": self.store.get_provenance_summary(),
            "output_txt": str(txt_file),
            "output_json": str(json_file),
        }
        logger.info(f"[EXPORT] Crawl complete! Saved {len(endpoints)} canonical endpoints to {txt_file} and {json_file}")
        return summary
