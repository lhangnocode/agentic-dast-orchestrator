"""
Pydantic Schemas for Structured LLM Orchestrator Decisions.
"""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class ToolActionEnum(str, Enum):
    ZAP_TRADITIONAL_SPIDER = "zap_traditional_spider"
    ZAP_AJAX_SPIDER = "zap_ajax_spider"
    WAPITI_FORM_CRAWL = "wapiti_form_crawl"
    FIRECRAWL_NAVIGATE = "firecrawl_navigate"
    TERMINATE = "terminate"


class OrchestratorDecision(BaseModel):
    action: ToolActionEnum = Field(
        description="The next specialized security crawler to invoke, or 'terminate' if exploration is saturated."
    )
    reasoning: str = Field(
        description="Tactical reasoning explaining why this tool is optimal given the current frontier and past marginal gains."
    )
    target_seeds: List[str] = Field(
        default_factory=list,
        description="List of target URL seeds to feed into the chosen tool. Must be selected from the pending frontier or base URL."
    )
    expected_discovery_type: Optional[str] = Field(
        default="",
        description="Expected type of discoveries: 'form_parameters', 'dynamic_js_routes', 'admin_panels', etc."
    )
