"""
Adaptive Crawl Orchestration Loop.
Coordinates the cognitive plane (Qwen / Alibaba Cloud), safety firewall,
deterministic tool wrappers (ZAP, Wapiti, Firecrawl), and SQLite state matrix.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm.qwen_client import QwenClient
from normalization.normalizer import normalize_endpoint
from orchestrator.compactor import StateCompactor
from orchestrator.decision_schema import OrchestratorDecision, ToolActionEnum
from orchestrator.safety_firewall import SafetyFirewall
from storage.state_store import StateStore
from tools.firecrawl_client import FirecrawlClient
from tools.wapiti_wrapper import WapitiWrapper
from tools.zap_wrapper import ZapWrapper

logger = logging.getLogger("orchestrator.loop")


class AdaptiveOrchestrator:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.target_config = config.get("target", {})
        self.base_url = self.target_config.get("base_url", "http://localhost/")
        self.allowed_hosts = list(self.target_config.get("allowed_hosts", ["localhost", "127.0.0.1"]))
        
        # Tunnel integration for Firecrawl Cloud
        self.public_tunnel_url = (os.getenv("PUBLIC_TUNNEL_URL") or self.target_config.get("public_tunnel_url") or "").rstrip("/")
        if self.public_tunnel_url:
            from urllib.parse import urlparse
            p_tun = urlparse(self.public_tunnel_url)
            if p_tun.hostname and p_tun.hostname not in self.allowed_hosts:
                self.allowed_hosts.append(p_tun.hostname)
                logger.info(f"[INIT] Added public tunnel host to firewall allowed scope: {p_tun.hostname}")

        self.orch_config = config.get("orchestrator", {})
        self.max_iterations = self.orch_config.get("max_iterations", 8)
        self.marginal_gain_threshold = self.orch_config.get("marginal_gain_threshold", 0.03)

        # Storage
        db_path = config.get("storage", {}).get("sqlite_db", "storage/orchestrator_state.db")
        self.store = StateStore(db_path=db_path)

        # Safety & Compaction
        self.firewall = SafetyFirewall(allowed_hosts=self.allowed_hosts, base_url=self.base_url)
        self.compactor = StateCompactor(max_sample_endpoints=self.orch_config.get("compactor_max_endpoints", 30))

        # LLM Client
        llm_cfg = config.get("llm", {})
        self.qwen_client = QwenClient(
            api_key=llm_cfg.get("api_key"),
            base_url=llm_cfg.get("base_url"),
            model=llm_cfg.get("model")
        )

        # Tools
        tools_cfg = config.get("tools", {})
        zap_cfg = tools_cfg.get("zap", {})
        self.zap = ZapWrapper(
            api_url=zap_cfg.get("api_url", "http://127.0.0.1:18080"),
            api_key=zap_cfg.get("api_key", "changeme")
        )

        wapiti_cfg = tools_cfg.get("wapiti", {})
        self.wapiti = WapitiWrapper(
            binary_path=wapiti_cfg.get("binary", "wapiti"),
            scope=wapiti_cfg.get("scope", "domain"),
            max_parameters=wapiti_cfg.get("max_parameters", 100),
            max_scan_time=wapiti_cfg.get("max_scan_time", 300),
            timeout=wapiti_cfg.get("timeout", 20),
            session_db_path=wapiti_cfg.get("session_file", "output/temp/wapiti/session.db"),
            report_path=wapiti_cfg.get("report_file", "output/temp/wapiti/report.json"),
            log_path=wapiti_cfg.get("log_file", "output/logs/wapiti.log"),
        )

        firecrawl_cfg = tools_cfg.get("firecrawl", {})
        self.firecrawl = FirecrawlClient(
            api_key=firecrawl_cfg.get("api_key"),
            api_url=firecrawl_cfg.get("api_url", "https://api.firecrawl.dev/v2"),
            model=firecrawl_cfg.get("model", "spark-2"),
            poll_timeout_seconds=firecrawl_cfg.get("poll_timeout_seconds", 600),
            poll_interval_seconds=firecrawl_cfg.get("poll_interval_seconds", 10),
            mock_fallback=True,
            public_tunnel_url=self.public_tunnel_url
        )

    def seed_initial_target(self) -> None:
        """Seeds the root target URL into state store if store is empty."""
        if self.store.get_total_endpoints_count() == 0:
            root_ep = normalize_endpoint(self.base_url, method="GET", source_tool="initial_seed", target_base=self.base_url)
            if root_ep:
                self.store.insert_or_update_endpoint(root_ep, current_iteration=0)
                logger.info(f"[INIT] Seeded target base URL: {self.base_url}")

    def run(self) -> Dict[str, Any]:
        """
        Executes the adaptive orchestration state machine loop.
        """
        logger.info(f"=== Starting Adaptive DAST Crawl Orchestrator on {self.base_url} ===")
        self.seed_initial_target()

        low_gain_consecutive_rounds = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for iteration in range(1, self.max_iterations + 1):
            endpoints_before = self.store.get_total_endpoints_count()
            logger.info(f"\n--- [Iteration {iteration}/{self.max_iterations}] Current Attack Surface: {endpoints_before} endpoints ---")

            # 1. State Compaction
            context_yaml = self.compactor.build_prompt_context(
                store=self.store,
                current_iteration=iteration,
                max_iterations=self.max_iterations,
                base_url=self.base_url
            )

            # 2. Cognitive Decision (Qwen / Alibaba Cloud)
            start_llm = time.time()
            decision, p_tok, c_tok = self.qwen_client.get_decision(
                context_yaml=context_yaml,
                store=self.store,
                current_iteration=iteration,
                base_url=self.base_url
            )
            total_prompt_tokens += p_tok
            total_completion_tokens += c_tok

            logger.info(f"[LLM Decision] Action: {decision.action.value}")
            logger.info(f"[LLM Reasoning] {decision.reasoning}")

            # 3. Check for deliberate termination
            if decision.action == ToolActionEnum.TERMINATE:
                logger.info("[STOP] Orchestrator decided to terminate exploration: surface is saturated.")
                break

            # 4. Scope Whitelist & Seed Sanitization (SSRF Protection)
            sanitized_seeds = self.firewall.sanitize_seeds(decision.target_seeds)
            logger.info(f"[Firewall] Sanitized {len(sanitized_seeds)} active seed(s): {sanitized_seeds[:3]}...")

            # 5. Tool Execution Plane
            tool_start = time.time()
            raw_results = self._dispatch_tool(decision.action, sanitized_seeds)
            tool_duration = time.time() - tool_start

            # 6. Normalization & State Store Update
            new_discovered = 0
            for item in raw_results:
                raw_url = item.get("url", "")
                method = item.get("method", "GET")
                status = item.get("status_code", 200)
                src = item.get("source", decision.action.value)

                norm = normalize_endpoint(raw_url, method=method, source_tool=src, status_code=status, target_base=self.base_url)
                if norm:
                    is_fresh = self.store.insert_or_update_endpoint(norm, current_iteration=iteration)
                    if is_fresh:
                        new_discovered += 1

            endpoints_after = self.store.get_total_endpoints_count()
            marginal_gain = new_discovered / max(endpoints_before, 1)

            logger.info(
                f"[Iteration {iteration} Complete] New Discoveries: {new_discovered} | "
                f"Total Endpoints: {endpoints_after} | Marginal Gain: {marginal_gain * 100:.1f}% | "
                f"Tool Duration: {tool_duration:.1f}s"
            )

            # 7. Record History Record
            self.store.record_iteration({
                "iteration": iteration,
                "chosen_tool": decision.action.value,
                "reasoning": decision.reasoning,
                "target_seeds": sanitized_seeds,
                "endpoints_before": endpoints_before,
                "endpoints_after": endpoints_after,
                "new_endpoints_discovered": new_discovered,
                "marginal_gain": marginal_gain,
                "tool_duration_seconds": tool_duration,
                "llm_prompt_tokens": p_tok,
                "llm_completion_tokens": c_tok
            })

            # 8. Adaptive Early Stopping Check
            if iteration >= 3 and marginal_gain < self.marginal_gain_threshold:
                low_gain_consecutive_rounds += 1
                if low_gain_consecutive_rounds >= 2:
                    logger.info(
                        f"[EARLY STOPPING] Marginal gain fell below {self.marginal_gain_threshold * 100:.1f}% "
                        f"for 2 consecutive iterations. Halting crawl."
                    )
                    break
            else:
                low_gain_consecutive_rounds = 0

        # Export final results
        final_summary = self.export_results()
        final_summary["total_prompt_tokens"] = total_prompt_tokens
        final_summary["total_completion_tokens"] = total_completion_tokens
        return final_summary

    def _dispatch_tool(self, action: ToolActionEnum, seeds: List[str]) -> List[Dict[str, Any]]:
        """Invokes the selected tool safely."""
        try:
            if action == ToolActionEnum.FIRECRAWL_NAVIGATE:
                target = seeds[0] if seeds else self.base_url
                return self.firecrawl.run_agent_discovery(target_url=target)

            elif action == ToolActionEnum.ZAP_TRADITIONAL_SPIDER:
                return self.zap.run_traditional_spider(seeds=seeds)

            elif action == ToolActionEnum.ZAP_AJAX_SPIDER:
                return self.zap.run_ajax_spider(seeds=seeds)

            elif action == ToolActionEnum.WAPITI_FORM_CRAWL:
                return self.wapiti.run_crawl_only(base_url=self.base_url, seeds=seeds)

            else:
                logger.warning(f"Unrecognized action: {action}")
                return []
        except Exception as e:
            logger.error(f"Error executing {action.value}: {e}")
            return []

    def export_results(self) -> Dict[str, Any]:
        """Saves canonical discovered endpoints to standard output files."""
        endpoints = self.store.get_all_endpoints()
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)

        txt_file = output_dir / "normalized_discovered_urls.txt"
        txt_v2_file = output_dir / "normalized_discovered_urls_v2.txt"
        json_file = output_dir / "normalized_endpoints.json"
        json_v2_file = output_dir / "normalized_endpoints_v2.json"

        # Unique normalized URLs
        unique_urls = sorted(list(dict.fromkeys(ep["normalized_url"] for ep in endpoints)))
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
