"""
Context Compactor for LLM Orchestration.
Compresses the relational state into a dense, token-efficient YAML abstract
ensuring prompt consumption remains under 1,000 tokens per turn.
"""

from typing import Any, Dict
import yaml

from storage.state_store import StateStore


class StateCompactor:
    def __init__(self, max_sample_endpoints: int = 15):
        self.max_sample_endpoints = max_sample_endpoints

    def build_prompt_context(
        self,
        store: StateStore,
        current_iteration: int,
        max_iterations: int,
        base_url: str
    ) -> str:
        """
        Creates a structured YAML summary of the current crawling progress.
        """
        summary = store.get_compact_summary(max_sample_endpoints=self.max_sample_endpoints)

        total_endpoints = summary.get("total_canonical_endpoints", 0)
        tool_matrix = summary.get("tool_coverage_matrix", {})
        provenance = summary.get("tool_provenance_contributions", {})
        pending_frontier = summary.get("pending_frontier_samples", {})
        latest_run = summary.get("latest_run")

        last_run_info = "None (Initial iteration)"
        if latest_run:
            last_run_info = {
                "iteration": latest_run.get("iteration"),
                "tool_used": latest_run.get("chosen_tool"),
                "new_discovered": latest_run.get("new_endpoints_discovered"),
                "marginal_gain": f"{latest_run.get('marginal_gain', 0.0) * 100:.1f}%",
                "reasoning_summary": (latest_run.get("reasoning") or "")[:120] + "..."
            }

        context_dict = {
            "mission": "DAST Adaptive Attack Surface & Endpoint Discovery",
            "target": base_url,
            "iteration": f"{current_iteration}/{max_iterations}",
            "attack_surface_metrics": {
                "total_canonical_endpoints": total_endpoints,
                "tool_provenance": provenance,
            },
            "tool_coverage_matrix": {
                tool: {
                    "pending": stats.get("PENDING", 0),
                    "processed": stats.get("PROCESSED", 0),
                    "failed": stats.get("FAILED", 0),
                }
                for tool, stats in tool_matrix.items()
            },
            "last_iteration_retrospective": last_run_info,
            "pending_frontier_candidates": {
                tool: seeds[:5] for tool, seeds in pending_frontier.items() if seeds
            },
            "recent_canonical_endpoints_sample": summary.get("recent_canonical_endpoints_sample", [])[:10]
        }

        yaml_str = yaml.dump(context_dict, sort_keys=False, default_flow_style=False)
        return yaml_str
