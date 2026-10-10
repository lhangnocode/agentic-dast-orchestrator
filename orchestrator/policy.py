"""
Deterministic, target-agnostic crawl policy (strategy 2: ZAP + Wapiti first).

- `classify_surface` turns raw surface counts into signals (SPA likelihood, auth walls)
  that both this policy and the LLM context use.
- `HeuristicPolicy.decide` picks the next tool after the bootstrap phase. It is used when
  no LLM is configured, when the LLM call fails, or when the LLM picks an unusable tool.
"""

from typing import Any, Dict, List

from orchestrator.decision_schema import OrchestratorDecision, ToolActionEnum
from storage.state_store import StateStore

AUTH_WALL_STATUSES = (301, 302, 303, 307, 308, 401, 403)


def classify_surface(profile: Dict[str, int], bootstrap_done: bool = True) -> Dict[str, Any]:
    """
    Generic SPA / auth-wall detection from the shape of what the classic crawlers found:
    - hash routes (/#/x) are only produced by client-side routers;
    - many API-like endpoints but few rendered pages means the UI is built in JS;
    - a traditional crawl that yields almost no HTML pages hit a JS shell.
    """
    signals: List[str] = []
    if profile.get("hash_routes", 0) > 0:
        signals.append("hash_routes")
    api, html = profile.get("api_like", 0), profile.get("html_pages", 0)
    if api >= 3 and api >= html:
        signals.append("api_heavy")
    if bootstrap_done and html <= 3:
        signals.append("few_html_pages")

    spa = "high" if len(signals) >= 2 else "medium" if signals else "low"
    walls = profile.get("auth_walls", 0) + profile.get("redirects", 0)
    return {"spa_likelihood": spa, "spa_signals": signals, "auth_walls_or_redirects": walls}


class HeuristicPolicy:
    def __init__(self, max_runs_per_tool: int = 2, seed_limit: int = 15):
        self.max_runs_per_tool = max_runs_per_tool
        self.seed_limit = seed_limit

    def decide(self, store: StateStore, available_tools: List[str], base_url: str) -> OrchestratorDecision:
        runs = store.get_tool_run_counts()
        surface = classify_surface(store.get_surface_profile())
        spa = surface["spa_likelihood"]

        def usable(tool: str) -> bool:
            return tool in available_tools and runs.get(tool, 0) < self.max_runs_per_tool

        def frontier(tool: str, statuses=None) -> List[str]:
            return store.get_pending_frontier(tool, limit=self.seed_limit, statuses=statuses)

        candidates: List[OrchestratorDecision] = []

        # 1. SPA: a real browser is the only way to execute the client-side router
        ajax_frontier = frontier(ToolActionEnum.ZAP_AJAX_SPIDER.value)
        ajax_first = runs.get(ToolActionEnum.ZAP_AJAX_SPIDER.value, 0) == 0
        if spa in ("high", "medium") and usable(ToolActionEnum.ZAP_AJAX_SPIDER.value) and (ajax_first or ajax_frontier):
            seeds = self._with_base(base_url, ajax_frontier, first_run=ajax_first)
            candidates.append(self._decision(
                ToolActionEnum.ZAP_AJAX_SPIDER, seeds, "dynamic_client_routes",
                f"SPA likelihood {spa} ({', '.join(surface['spa_signals'])}): run the headless-browser Ajax Spider "
                f"to execute client-side routing and DOM events."))

        # 2. Pages the classic crawlers could not open (auth walls / redirects) -> semantic UI agent
        if usable(ToolActionEnum.FIRECRAWL_NAVIGATE.value) and (surface["auth_walls_or_redirects"] > 0 or spa == "high"):
            walled = frontier(ToolActionEnum.FIRECRAWL_NAVIGATE.value, statuses=AUTH_WALL_STATUSES)
            fc_first = runs.get(ToolActionEnum.FIRECRAWL_NAVIGATE.value, 0) == 0
            seeds = walled or (self._with_base(base_url, frontier(ToolActionEnum.FIRECRAWL_NAVIGATE.value), first_run=True) if fc_first else [])
            if seeds:
                focus = ("pages that redirected or denied the classic crawlers" if walled
                         else "menus and client-side views not reachable via plain links")
                candidates.append(self._decision(
                    ToolActionEnum.FIRECRAWL_NAVIGATE, seeds, "gated_or_js_views",
                    f"Targeted semantic navigation on {len(seeds)} seed(s): {focus}.", focus=focus))

        # 3. New pages found by other tools may hold forms/parameters Wapiti has not parsed
        wapiti_seeds = frontier(ToolActionEnum.WAPITI_FORM_CRAWL.value)
        if wapiti_seeds and usable(ToolActionEnum.WAPITI_FORM_CRAWL.value):
            candidates.append(self._decision(
                ToolActionEnum.WAPITI_FORM_CRAWL, wapiti_seeds, "form_parameters",
                f"{len(wapiti_seeds)} page(s) not yet parsed by Wapiti: extract forms and parameters."))

        # 4. Link-spider the routes contributed by browser-based tools
        zap_seeds = frontier(ToolActionEnum.ZAP_TRADITIONAL_SPIDER.value)
        if zap_seeds and usable(ToolActionEnum.ZAP_TRADITIONAL_SPIDER.value):
            candidates.append(self._decision(
                ToolActionEnum.ZAP_TRADITIONAL_SPIDER, zap_seeds, "hyperlinks_and_static_routes",
                f"{len(zap_seeds)} newly found page(s) not yet spidered: follow their links."))

        # 5. Even classic multi-page apps attach routes to JS events: one browser pass if never done
        if spa == "low" and runs.get("zap_ajax_spider", 0) == 0 and usable(ToolActionEnum.ZAP_AJAX_SPIDER.value):
            candidates.append(self._decision(
                ToolActionEnum.ZAP_AJAX_SPIDER, [base_url], "dynamic_client_routes",
                "Single headless-browser pass to catch JS-triggered routes the link spider cannot see."))

        if candidates:
            return candidates[0]

        return OrchestratorDecision(
            action=ToolActionEnum.TERMINATE,
            reasoning="No usable tool has an unvisited frontier left (or all reached their run budget).",
            target_seeds=[],
            expected_discovery_type="complete",
        )

    @staticmethod
    def _with_base(base_url: str, seeds: List[str], first_run: bool) -> List[str]:
        return list(dict.fromkeys(([base_url] if first_run else []) + seeds)) or [base_url]

    @staticmethod
    def _decision(action: ToolActionEnum, seeds: List[str], kind: str, reasoning: str,
                  focus: str = "") -> OrchestratorDecision:
        return OrchestratorDecision(
            action=action, reasoning=reasoning, target_seeds=seeds,
            expected_discovery_type=kind, focus=focus,
        )
