"""
Alibaba Cloud Model Studio / Qwen LLM Client via OpenAI-Compatible SDK.
Provides structured decision making for DAST crawl orchestration.

This client only talks to the model. When it is not configured or the call fails it
returns None and the orchestrator falls back to orchestrator.policy.HeuristicPolicy.
"""

import logging
import os
from typing import Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI

from orchestrator.decision_schema import OrchestratorDecision

load_dotenv()
logger = logging.getLogger("orchestrator.qwen")

SYSTEM_ORCHESTRATOR_PROMPT = """
You orchestrate crawl-only discovery tools to map the routes and parameters of ONE authorized
web application while avoiding redundant requests. You never attack; tools only crawl.

The fast classic crawlers (ZAP traditional spider + Wapiti) have already run as a bootstrap.
You now decide, one step at a time, which tool adds the most NEW canonical endpoints next.

Tools (only choose ones listed in `available_tools`):
- zap_traditional_spider: cheap link/sitemap spider. Good for newly found server-rendered pages.
- wapiti_form_crawl: parses forms and parameters of pages. Good for new pages with inputs.
- zap_ajax_spider: real headless browser (executes JS, clicks elements). The main tool for SPAs:
  hash routes (/#/...), API-heavy surfaces, or very few HTML pages found by the classic crawlers.
- firecrawl_navigate: slow, expensive semantic UI agent. Use it sparingly and targeted: on pages
  that redirect/deny classic crawlers (3xx/401/403) or on JS menus no other tool opened.
  Put a short instruction for it in `focus`.
- terminate: when recent marginal gain is ~0 and no tool has a useful unvisited frontier.

Rules:
- Pick target_seeds from `pending_frontier_candidates` of the chosen tool, or the target URL.
- Do not repeat a tool that just produced ~0 new endpoints unless its frontier grew since.
- Prefer cheap tools when they still have frontier; justify expensive ones in `reasoning`.
- Return valid structured JSON matching the OrchestratorDecision schema.
"""


class QwenClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = os.getenv("DASHSCOPE_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        self.model = model or os.getenv("QWEN_MODEL", "qwen-plus")

        self.client: Optional[OpenAI] = None
        if self.is_configured():
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "your_dashscope_api_key_here")

    def get_decision(self, context_yaml: str) -> Optional[Tuple[OrchestratorDecision, int, int]]:
        """
        Requests the next crawling decision from Qwen.
        Returns (decision, prompt_tokens, completion_tokens), or None if unavailable/failed.
        """
        if self.client is None:
            return None

        user_content = f"Current Orchestration State:\n```yaml\n{context_yaml}\n```\nWhat is the next optimal tool action?"
        try:
            completion = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_ORCHESTRATOR_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                response_format=OrchestratorDecision,
                temperature=0.2,
            )
            decision = completion.choices[0].message.parsed
            if decision is None:
                raise ValueError("Parsed decision is None from LLM response.")

            prompt_tokens = completion.usage.prompt_tokens if completion.usage else 0
            completion_tokens = completion.usage.completion_tokens if completion.usage else 0
            logger.info(f"[LLM] Qwen decision: action={decision.action.value}, seeds={len(decision.target_seeds)}")
            return decision, prompt_tokens, completion_tokens

        except Exception as e:
            logger.warning(f"[LLM] Qwen API call failed: {e}")
            return None
