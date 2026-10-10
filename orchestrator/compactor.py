"""
Context Compactor for LLM Orchestration.
Compresses the relational state into a dense, token-efficient YAML abstract
ensuring prompt consumption remains under ~1,000 tokens per turn.
"""

from typing import Any, Dict, List, Optional

import yaml

from orchestrator.policy import classify_surface
from storage.state_store import StateStore


class StateCompactor:
    def __init__(self, max_sample_endpoints: int = 15):
        self.max_sample_endpoints = max_sample_endpoints

    def build_prompt_context(
        self,
        store: StateStore,
        current_iteration: int,
        max_iterations: int,
        base_url: str,
        available_tools: Optional[List[str]] = None,
    ) -> str:
        """Creates a structured YAML summary of the current crawling progress."""
        summary = store.get_compact_summary(max_sample_endpoints=self.max_sample_endpoints)
        surface = summary.get("surface_profile", {})

        recent_runs = [
            {
                "iteration": r.get("iteration"),
                "tool": r.get("chosen_tool"),
                "new_endpoints": r.get("new_endpoints_discovered"),
                "marginal_gain": f"{(r.get('marginal_gain') or 0.0) * 100:.1f}%",
            }
            for r in summary.get("recent_runs", [])
        ]

        context_dict: Dict[str, Any] = {
            "mission": "Adaptive Web Application Route & Feature Discovery",
            "target": base_url,
            "iteration": f"{current_iteration}/{max_iterations}",
            "available_tools": available_tools if available_tools is not None else "unknown",
            "route_coverage_metrics": {
                "total_canonical_endpoints": summary.get("total_canonical_endpoints", 0),
                "tool_provenance": summary.get("tool_provenance_contributions", {}),
            },
            "surface_profile": {
                **{k: surface.get(k, 0) for k in (
                    "html_pages", "api_like", "hash_routes", "parameterized",
                    "non_get", "auth_walls", "redirects", "errors")},
                **classify_surface(surface),
            },
            "tool_runs_so_far": summary.get("tool_run_counts", {}),
            "tool_coverage_matrix": {
                tool: {"pending": stats.get("PENDING", 0), "processed": stats.get("PROCESSED", 0)}
                for tool, stats in summary.get("tool_coverage_matrix", {}).items()
            },
            "recent_runs": recent_runs or "None (initial iteration)",
            "pending_frontier_candidates": {
                tool: seeds[:5] for tool, seeds in summary.get("pending_frontier_samples", {}).items() if seeds
            },
            "recent_canonical_endpoints_sample": summary.get("recent_canonical_endpoints_sample", [])[:10],
        }
        return yaml.dump(context_dict, sort_keys=False, default_flow_style=False)
