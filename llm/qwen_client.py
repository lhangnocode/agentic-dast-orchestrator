"""
Alibaba Cloud Model Studio / Qwen LLM Client via OpenAI-Compatible SDK.
Provides structured decision making for DAST crawl orchestration.
"""

import json
import logging
import os
from typing import Optional, Tuple
from openai import OpenAI
from pydantic import ValidationError

from orchestrator.decision_schema import OrchestratorDecision, ToolActionEnum
from storage.state_store import StateStore

logger = logging.getLogger("orchestrator.qwen")

SYSTEM_ORCHESTRATOR_PROMPT = """
You are the Chief DAST Crawling & Discovery Orchestrator.
Your goal is to maximize the discovery of unique, authenticated attack surfaces, endpoints,
and parameter configurations on the target application while avoiding redundant HTTP requests.

Available Specialized Tools:
1. `firecrawl_navigate`: Best for semantic UI exploration, expanding administrative dropdowns, modals, and client routes.
2. `zap_traditional_spider`: High-speed link crawling through HTML anchors and sitemaps.
3. `zap_ajax_spider`: Headless browser spidering dynamic single-page applications and DOM events.
4. `wapiti_form_crawl`: Form structure extraction and parameter discovery/reflection without attack payloads.
5. `terminate`: Terminate the crawl when all valuable frontiers are saturated or marginal gain drops to near zero.

Strategy Guidelines:
- Begin by exploring unknown UI territories or running foundational spidering.
- When new administrative panels or UI routes are uncovered, route them to ZAP or Wapiti to spider internal forms and parameters.
- When form-rich pages (e.g. managerUsers, managerVideos) are discovered, use `wapiti_form_crawl`.
- When multiple tools show zero marginal gain or pending frontier is empty, decide `terminate`.
- Return valid structured JSON adhering to the OrchestratorDecision schema.
"""


class QwenClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = base_url or os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        self.model = model or os.getenv("QWEN_MODEL", "qwen-plus")

        self.client: Optional[OpenAI] = None
        if self.is_configured():
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url
            )

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "your_dashscope_api_key_here")

    def get_decision(
        self,
        context_yaml: str,
        store: StateStore,
        current_iteration: int,
        base_url: str
    ) -> Tuple[OrchestratorDecision, int, int]:
        """
        Requests the next tactical crawling decision from Qwen.
        Returns: (OrchestratorDecision, prompt_tokens, completion_tokens).
        """
        if not self.is_configured() or self.client is None:
            logger.info("[LLM] DashScope API key not set. Using deterministic heuristic orchestrator.")
            return self._heuristic_decision(store, current_iteration, base_url), 0, 0

        user_content = f"Current Orchestration State:\n```yaml\n{context_yaml}\n```\nWhat is the next optimal tool action?"

        try:
            # Call using OpenAI beta parse for structured Pydantic response
            completion = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_ORCHESTRATOR_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                response_format=OrchestratorDecision,
                temperature=0.2,
            )

            prompt_tokens = completion.usage.prompt_tokens if completion.usage else 0
            completion_tokens = completion.usage.completion_tokens if completion.usage else 0

            decision = completion.choices[0].message.parsed
            if decision is None:
                raise ValueError("Parsed decision is None from LLM response.")

            logger.info(f"[LLM] Qwen decision: action={decision.action.value}, seeds={len(decision.target_seeds)}")
            return decision, prompt_tokens, completion_tokens

        except Exception as e:
            logger.warning(f"[LLM] Qwen API call failed: {e}. Falling back to heuristic decision.")
            return self._heuristic_decision(store, current_iteration, base_url), 0, 0

    def _heuristic_decision(
        self,
        store: StateStore,
        current_iteration: int,
        base_url: str
    ) -> OrchestratorDecision:
        """
        Deterministic research baseline for offline execution and regression testing:
        Iter 1: Firecrawl UI navigation to discover admin dropdowns and hidden routes.
        Iter 2: ZAP Traditional Spider to exhaustively spider links discovered by Firecrawl.
        Iter 3: Wapiti Form Crawl to extract and reflect parameters on discovered manager pages.
        Iter 4: ZAP Ajax Spider to crawl dynamic DOM elements and client SPA routes.
        Iter >= 5: Terminate if pending frontier is low or saturated.
        """
        if current_iteration == 1:
            return OrchestratorDecision(
                action=ToolActionEnum.FIRECRAWL_NAVIGATE,
                reasoning="Initial state: Firecrawl UI agent explores authenticated admin dropdowns, menus, and hidden panels.",
                target_seeds=[base_url],
                expected_discovery_type="admin_panels_and_menus"
            )

        if current_iteration == 2:
            pending_zap = store.get_pending_frontier("zap_traditional_spider", limit=15)
            seeds = pending_zap if pending_zap else [base_url]
            return OrchestratorDecision(
                action=ToolActionEnum.ZAP_TRADITIONAL_SPIDER,
                reasoning="Second phase: ZAP Traditional Spider maps out link graphs starting from semantic routes discovered by Firecrawl.",
                target_seeds=seeds,
                expected_discovery_type="hyperlinks_and_static_routes"
            )

        if current_iteration == 3:
            pending_wapiti = store.get_pending_frontier("wapiti_form_crawl", limit=15)
            seeds = pending_wapiti if pending_wapiti else [base_url]
            return OrchestratorDecision(
                action=ToolActionEnum.WAPITI_FORM_CRAWL,
                reasoning="Third phase: Wapiti form specialist inspects input forms, user groups, and manager endpoints to discover query/POST parameters.",
                target_seeds=seeds,
                expected_discovery_type="form_parameters"
            )

        if current_iteration == 4:
            pending_ajax = store.get_pending_frontier("zap_ajax_spider", limit=10)
            seeds = pending_ajax if pending_ajax else [base_url]
            return OrchestratorDecision(
                action=ToolActionEnum.ZAP_AJAX_SPIDER,
                reasoning="Fourth phase: ZAP Ajax Spider executes headless DOM exploration on remaining interactive endpoints.",
                target_seeds=seeds,
                expected_discovery_type="dynamic_client_routes"
            )

        return OrchestratorDecision(
            action=ToolActionEnum.TERMINATE,
            reasoning="All specialized crawler capabilities have explored the attack surface. Frontier is saturated.",
            target_seeds=[],
            expected_discovery_type="complete"
        )
