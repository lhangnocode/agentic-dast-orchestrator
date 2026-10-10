"""
Adaptive Crawl Orchestration Loop (strategy 2: classic crawlers first).

  Phase 1 - Bootstrap (deterministic, no LLM):
            round 1: ZAP (traditional + Ajax spider) and Wapiti crawl from the base URL;
            round 2 (exchange): each engine is re-seeded with ALL URLs the other engine found
            (ZAP <- Wapiti, Wapiti <- ZAP), without any seed limit.
            Tools of the same engine run one after another (they share ZAP's history);
            different engines run in parallel when `bootstrap.parallel` is true.
  Phase 2 - Adaptive rounds: the LLM (or HeuristicPolicy as fallback) picks the next tool
            from the compacted state - typically the Ajax Spider for SPAs and Firecrawl
            only on what the classic crawlers could not open.

Every run is normalized into the SQLite store, and the coverage matrix is updated so a tool
is never re-seeded with pages it already visited.
"""

import json
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from llm.qwen_client import QwenClient
from normalization.normalizer import NormalizationProfile, normalize_endpoint
from orchestrator.compactor import StateCompactor
from orchestrator.decision_schema import OrchestratorDecision, ToolActionEnum
from orchestrator.policy import HeuristicPolicy
from orchestrator.safety_firewall import SafetyFirewall
from storage.state_store import StateStore
from tools.registry import ToolRegistry, ToolRun, WAPITI, ZAP_AJAX, ZAP_TRADITIONAL

logger = logging.getLogger("orchestrator.loop")

DEFAULT_BOOTSTRAP_TOOLS = [ZAP_TRADITIONAL, ZAP_AJAX, WAPITI]


def engine_of(tool: str) -> str:
    """'zap_ajax_spider' -> 'zap': tools of one engine share state and must not run concurrently."""
    return tool.split("_", 1)[0]


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
        self.max_iterations = self.orch_config.get("max_iterations", 8)
        self.marginal_gain_threshold = self.orch_config.get("marginal_gain_threshold", 0.03)
        self.patience = self.orch_config.get("early_stop_patience", 2)
        bootstrap_cfg = self.orch_config.get("bootstrap", {})
        self.bootstrap_tools: List[str] = list(bootstrap_cfg.get("tools", DEFAULT_BOOTSTRAP_TOOLS))
        self.bootstrap_parallel: bool = bootstrap_cfg.get("parallel", True)
        self.exchange_round: bool = bootstrap_cfg.get("exchange_round", True)

        db_path = config.get("storage", {}).get("sqlite_db", "storage/orchestrator_state.db")
        self.store = StateStore(db_path=db_path)
        self.profile = NormalizationProfile.from_config(config)

        self.firewall = SafetyFirewall(allowed_hosts=self.allowed_hosts, base_url=self.base_url)
        self.compactor = StateCompactor(max_sample_endpoints=self.orch_config.get("compactor_max_endpoints", 30))
        self.policy = HeuristicPolicy(
            max_runs_per_tool=self.orch_config.get("max_runs_per_tool", 2),
            seed_limit=self.orch_config.get("seed_limit", 15),
        )

        llm_cfg = config.get("llm", {})
        self.qwen_client = QwenClient(
            api_key=llm_cfg.get("api_key"),
            base_url=llm_cfg.get("base_url"),
            model=llm_cfg.get("model")
        )

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
        logger.info(f"=== Starting Adaptive DAST Crawl Orchestrator on {self.base_url} ===")
        self.seed_initial_target()

        available = self.tools.available_tools()
        logger.info(f"[INIT] Available tools: {available or 'none'}")

        totals = {"prompt_tokens": 0, "completion_tokens": 0, "requests_sent": 0}
        decision_sources = {"bootstrap": 0, "exchange": 0, "llm": 0, "heuristic": 0}
        iteration = 0

        # Phase 1: deterministic bootstrap with the classic crawlers
        bootstrap = [t for t in self.bootstrap_tools if t in available]
        skipped = [t for t in self.bootstrap_tools if t not in available]
        if skipped:
            logger.warning(f"[BOOTSTRAP] Skipping unavailable tool(s): {skipped}")
        if bootstrap and self.max_iterations > 0:
            iteration = 1
            for run in self._run_bootstrap(bootstrap, iteration):
                totals["requests_sent"] += run.requests_sent
                decision_sources["bootstrap"] += 1
            if self.exchange_round and self.max_iterations > 1 and len({engine_of(t) for t in bootstrap}) > 1:
                iteration = 2
                for run in self._run_exchange(bootstrap, iteration):
                    totals["requests_sent"] += run.requests_sent
                    decision_sources["exchange"] += 1

        # Phase 2: adaptive rounds
        low_gain_rounds = 0
        while iteration < self.max_iterations:
            iteration += 1
            decision, source, p_tok, c_tok = self._decide(iteration, available)
            totals["prompt_tokens"] += p_tok
            totals["completion_tokens"] += c_tok
            decision_sources[source] += 1
            logger.info(f"[Decision:{source}] {decision.action.value} - {decision.reasoning}")

            if decision.action == ToolActionEnum.TERMINATE:
                logger.info("[STOP] Orchestrator decided to terminate: surface is saturated.")
                break

            seeds = self.firewall.sanitize_seeds(decision.target_seeds)
            endpoints_before = self.store.get_total_endpoints_count()
            run = self.tools.run(decision.action.value, seeds, base_url=self.base_url, focus=decision.focus or "")
            new = self._ingest(run, iteration)
            totals["requests_sent"] += run.requests_sent
            marginal_gain = new / max(endpoints_before, 1)
            self._record(run, iteration, endpoints_before, new, decision.reasoning, p_tok, c_tok)

            if marginal_gain < self.marginal_gain_threshold:
                low_gain_rounds += 1
                if low_gain_rounds >= self.patience:
                    logger.info(
                        f"[EARLY STOPPING] Marginal gain below {self.marginal_gain_threshold * 100:.1f}% "
                        f"for {self.patience} consecutive adaptive round(s). Halting crawl."
                    )
                    break
            else:
                low_gain_rounds = 0

        summary = self.export_results()
        summary["total_prompt_tokens"] = totals["prompt_tokens"]
        summary["total_completion_tokens"] = totals["completion_tokens"]
        summary["total_requests_sent"] = totals["requests_sent"]
        summary["decision_sources"] = decision_sources
        summary["iterations_run"] = iteration
        return summary

    # ------------------------------------------------------------------ phases
    def _run_bootstrap(self, tools: List[str], iteration: int) -> List[ToolRun]:
        """Round 1: every bootstrap tool crawls from the base URL (+ configured entry points)."""
        seeds = self.firewall.sanitize_seeds([self.base_url] + self.extra_seeds)
        logger.info(f"\n--- [Iteration {iteration}/{self.max_iterations}] Bootstrap: {tools} ---")
        return self._run_plan([(t, seeds) for t in tools], iteration,
                              "Bootstrap: classic crawler baseline of the surface.")

    def _run_exchange(self, tools: List[str], iteration: int) -> List[ToolRun]:
        """
        Round 2: each tool is seeded with every URL found by the OTHER engine(s) that it has
        not visited yet (ZAP tools <- Wapiti's URLs, Wapiti <- ZAP's URLs). No seed limit.
        Seeds are computed before anything runs, so the exchange is symmetric.
        """
        plan = []
        for tool in tools:
            others = [t for t in tools if engine_of(t) != engine_of(tool)]
            seeds = self.store.get_pending_frontier(tool, limit=None, sources=others)
            seeds = [s for s in seeds if self.firewall.is_in_scope(s)]
            if seeds:
                plan.append((tool, seeds))
            logger.info(f"[EXCHANGE] {tool} <- {len(seeds)} URL(s) discovered by {sorted({engine_of(t) for t in others})}")

        if not plan:
            logger.info("[EXCHANGE] Nothing to exchange: both engines found the same URLs.")
            return []
        logger.info(f"\n--- [Iteration {iteration}/{self.max_iterations}] Exchange round: {[t for t, _ in plan]} ---")
        return self._run_plan(plan, iteration, "Exchange: re-seeded with the other engine's discoveries.")

    def _run_plan(self, plan: List[Tuple[str, List[str]]], iteration: int, reasoning: str) -> List[ToolRun]:
        """
        Runs (tool, seeds) pairs. Tools of the same engine run sequentially in one lane;
        lanes run in parallel if configured. Results are ingested in plan order afterwards.
        """
        lanes: "OrderedDict[str, List[Tuple[str, List[str]]]]" = OrderedDict()
        for tool, seeds in plan:
            lanes.setdefault(engine_of(tool), []).append((tool, seeds))

        def run_lane(items: List[Tuple[str, List[str]]]) -> List[ToolRun]:
            return [self.tools.run(tool, seeds, self.base_url) for tool, seeds in items]

        if self.bootstrap_parallel and len(lanes) > 1:
            with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
                lane_runs = list(pool.map(run_lane, lanes.values()))
        else:
            lane_runs = [run_lane(items) for items in lanes.values()]
        runs_by_tool = {run.tool: run for runs in lane_runs for run in runs}

        runs = []
        for tool, _ in plan:
            run = runs_by_tool[tool]
            before = self.store.get_total_endpoints_count()
            new = self._ingest(run, iteration)
            self._record(run, iteration, before, new, reasoning)
            runs.append(run)
        return runs

    def _decide(self, iteration: int, available: List[str]) -> Tuple[OrchestratorDecision, str, int, int]:
        """LLM decision when possible and valid, HeuristicPolicy otherwise."""
        if self.qwen_client.is_configured():
            context_yaml = self.compactor.build_prompt_context(
                store=self.store,
                current_iteration=iteration,
                max_iterations=self.max_iterations,
                base_url=self.base_url,
                available_tools=available,
            )
            result = self.qwen_client.get_decision(context_yaml)
            if result is not None:
                decision, p_tok, c_tok = result
                if decision.action == ToolActionEnum.TERMINATE or decision.action.value in available:
                    if decision.action != ToolActionEnum.TERMINATE and not decision.target_seeds:
                        decision.target_seeds = self.store.get_pending_frontier(
                            decision.action.value, limit=self.policy.seed_limit) or [self.base_url]
                    return decision, "llm", p_tok, c_tok
                logger.warning(f"[LLM] Chose unavailable tool {decision.action.value}; using heuristic policy.")
                return self.policy.decide(self.store, available, self.base_url), "heuristic", p_tok, c_tok

        return self.policy.decide(self.store, available, self.base_url), "heuristic", 0, 0

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
            f"requests: {run.requests_sent} | {run.duration_seconds:.1f}s"
            + (f" | error: {run.error}" if run.error else "")
        )
        return new

    def _record(self, run: ToolRun, iteration: int, endpoints_before: int, new: int, reasoning: str,
                prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.store.record_iteration({
            "iteration": iteration,
            "chosen_tool": run.tool,
            "reasoning": reasoning,
            "target_seeds": run.seeds,
            "endpoints_before": endpoints_before,
            "endpoints_after": self.store.get_total_endpoints_count(),
            "new_endpoints_discovered": new,
            "marginal_gain": new / max(endpoints_before, 1),
            "tool_duration_seconds": run.duration_seconds,
            "llm_prompt_tokens": prompt_tokens,
            "llm_completion_tokens": completion_tokens,
            "requests_sent": run.requests_sent,
        })

    # ------------------------------------------------------------------ export
    def export_results(self) -> Dict[str, Any]:
        """Saves canonical discovered endpoints to standard output files."""
        endpoints = self.store.get_all_endpoints()
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)

        txt_file = output_dir / "normalized_discovered_urls.txt"
        txt_v2_file = output_dir / "normalized_discovered_urls_v2.txt"
        json_file = output_dir / "normalized_endpoints.json"
        json_v2_file = output_dir / "normalized_endpoints_v2.json"

        unique_urls = sorted(dict.fromkeys(ep["normalized_url"] for ep in endpoints))
        for target_txt in (txt_file, txt_v2_file):
            with open(target_txt, "w", encoding="utf-8") as f:
                for u in unique_urls:
                    f.write(f"{u}\n")

        for target_json in (json_file, json_v2_file):
            with open(target_json, "w", encoding="utf-8") as f:
                json.dump(endpoints, f, indent=2)

        summary = {
            "total_canonical_endpoints": len(endpoints),
            "unique_normalized_urls": len(unique_urls),
            "tool_provenance": self.store.get_provenance_summary(),
            "output_txt": str(txt_file),
            "output_txt_v2": str(txt_v2_file),
            "output_json": str(json_file),
            "output_json_v2": str(json_v2_file),
        }
        logger.info(f"[EXPORT] Crawl complete! Saved {len(endpoints)} canonical endpoints to {txt_file} and {txt_v2_file}")
        return summary
