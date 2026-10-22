"""
Deterministic rule engine that picks the next crawl actions (no LLM).

`build_snapshot` reads everything the rules need from the StateStore once per round;
`select_actions` is a pure function of that Snapshot, so it can be tested without any tool.

Each rule closes a capability gap (see docs/tool-capabilities.md):

  R1 retry          last run of a tool failed/timed out and retries are left -> same tool, same seeds
  R2 first_run      an enabled tool that the fixed stages did not run has never run -> base URL
  R3 browser_spider browser spider's frontier changed since its last run -> new page-like URLs
  R4 link_spider    ZAP traditional spider's frontier changed -> new URLs
  R5 form_crawler   Wapiti's frontier changed -> new page-like URLs
  R6 ui_agent       Firecrawl's frontier changed -> new page-like URLs
  R7 probe          URLs with unknown HTTP status changed -> HTTP probe

Rules are evaluated per engine (zap / wapiti / firecrawl / http) in the order above; the first
match wins, so each round runs at most one action per engine (engines run in parallel lanes).
"Frontier changed" compares the tool's current pending-frontier fingerprint with the one
stored right after its last run, which guarantees termination on a surface that stops growing.
No action for any engine = saturation.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from orchestrator.policy import classify_surface
from storage.state_store import StateStore

ZAP_TRADITIONAL = "zap_traditional_spider"
ZAP_AJAX = "zap_ajax_spider"
ZAP_CLIENT = "zap_client_spider"
WAPITI = "wapiti_form_crawl"
FIRECRAWL = "firecrawl_navigate"
HTTP_PROBE = "http_probe"

RULE_ORDER = ("R1_retry", "R2_first_run", "R3_browser_spider", "R4_link_spider",
              "R5_form_crawler", "R6_ui_agent", "R7_probe")

# Which seeds each tool can use: browsers/agents and Wapiti want pages, not JSON or files
PAGE_LIKE_TOOLS = {ZAP_CLIENT, ZAP_AJAX, WAPITI, FIRECRAWL}


def engine_of(tool: str) -> str:
    """'zap_client_spider' -> 'zap': tools of one engine share state and never run concurrently."""
    return tool.split("_", 1)[0]


@dataclass(frozen=True)
class ToolState:
    tool: str
    runs: int
    last_status: Optional[str]          # ok | failed | timeout | None (never ran)
    last_seeds: List[str]
    consecutive_failures: int
    stored_fingerprint: Optional[str]   # pending frontier right after the last run
    current_fingerprint: str
    frontier: List[str]                 # usable seeds now (filtered for this tool)

    @property
    def frontier_changed(self) -> bool:
        return self.runs > 0 and self.current_fingerprint != self.stored_fingerprint and bool(self.frontier)


@dataclass(frozen=True)
class Snapshot:
    iteration: int
    base_seeds: List[str]
    tools: Dict[str, ToolState]         # only selectable, available tools
    staged_tools: Set[str]              # tools the fixed stages already cover
    surface: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    tool: str
    seeds: List[str]
    rule_id: str
    reason: str
    focus: str = ""


@dataclass
class RuleSettings:
    browser_spider: str = ZAP_CLIENT    # or zap_ajax_spider (A/B, rollback)
    max_retries: int = 1
    max_runs_per_tool: int = 10
    disabled_rules: Set[str] = field(default_factory=set)
    spa_html_page_max: int = 3          # surface signal threshold (logging only); calibrate on benchmarks

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "RuleSettings":
        r = config.get("orchestrator", {}).get("rules", {})
        return cls(
            browser_spider=r.get("browser_spider", ZAP_CLIENT),
            max_retries=r.get("max_retries", 1),
            max_runs_per_tool=r.get("max_runs_per_tool", 10),
            disabled_rules=set(r.get("disabled_rules", [])),
            spa_html_page_max=r.get("spa_html_page_max", 3),
        )

    def selectable_tools(self) -> List[str]:
        return [ZAP_TRADITIONAL, self.browser_spider, WAPITI, FIRECRAWL, HTTP_PROBE]


def tool_frontier(store: StateStore, tool: str) -> List[str]:
    if tool == HTTP_PROBE:
        return store.get_unknown_status_urls()
    return store.get_pending_frontier(tool, limit=None, page_like=tool in PAGE_LIKE_TOOLS)


def build_snapshot(store: StateStore, available: Iterable[str], settings: RuleSettings,
                   base_seeds: List[str], staged_tools: Set[str], iteration: int) -> Snapshot:
    available = set(available)
    last_runs = store.get_last_runs()
    run_counts = store.get_tool_run_counts()
    tools: Dict[str, ToolState] = {}
    for tool in settings.selectable_tools():
        if tool not in available:
            continue
        last = last_runs.get(tool) or {}
        tools[tool] = ToolState(
            tool=tool,
            runs=run_counts.get(tool, 0),
            last_status=last.get("status"),
            last_seeds=_json_list(last.get("target_seeds")),
            consecutive_failures=store.get_consecutive_failures(tool) if last else 0,
            stored_fingerprint=last.get("frontier_fingerprint"),
            current_fingerprint=store.frontier_fingerprint(tool),
            frontier=tool_frontier(store, tool),
        )
    return Snapshot(iteration=iteration, base_seeds=list(base_seeds), tools=tools,
                    staged_tools=set(staged_tools),
                    surface=classify_surface(store.get_surface_profile(), settings.spa_html_page_max))


def _json_list(value: Any) -> List[str]:
    import json
    try:
        data = json.loads(value) if isinstance(value, str) else (value or [])
        return [str(x) for x in data]
    except ValueError:
        return []


# ---------------------------------------------------------------------------- rules
def _r1_retry(t: ToolState, snap: Snapshot, s: RuleSettings) -> Optional[Action]:
    if t.last_status in ("failed", "timeout") and t.consecutive_failures <= s.max_retries and t.last_seeds:
        return Action(t.tool, t.last_seeds, "R1_retry",
                      f"last run ended '{t.last_status}' ({t.consecutive_failures}/{s.max_retries + 1} attempts)")
    return None


def _r2_first_run(t: ToolState, snap: Snapshot, s: RuleSettings) -> Optional[Action]:
    if t.runs == 0 and t.tool not in snap.staged_tools and t.tool != HTTP_PROBE:
        return Action(t.tool, snap.base_seeds, "R2_first_run", "enabled tool not covered by the stages has never run")
    return None


def _grew(rule_id: str, tool_filter, reason: str, focus: str = ""):
    def rule(t: ToolState, snap: Snapshot, s: RuleSettings) -> Optional[Action]:
        if tool_filter(t.tool, s) and t.frontier_changed:
            return Action(t.tool, t.frontier, rule_id, f"{reason} ({len(t.frontier)} new seed(s))", focus)
        return None
    return rule


def _r7_probe(t: ToolState, snap: Snapshot, s: RuleSettings) -> Optional[Action]:
    if t.tool == HTTP_PROBE and t.frontier and t.current_fingerprint != t.stored_fingerprint:
        return Action(t.tool, t.frontier, "R7_probe", f"{len(t.frontier)} URL(s) with unknown HTTP status")
    return None


RULES = {
    "R1_retry": _r1_retry,
    "R2_first_run": _r2_first_run,
    "R3_browser_spider": _grew("R3_browser_spider", lambda tool, s: tool == s.browser_spider,
                               "browser spider: new page-like URLs since its last run"),
    "R4_link_spider": _grew("R4_link_spider", lambda tool, s: tool == ZAP_TRADITIONAL,
                            "link spider: new URLs since its last run"),
    "R5_form_crawler": _grew("R5_form_crawler", lambda tool, s: tool == WAPITI,
                             "form crawler: new pages since its last run"),
    "R6_ui_agent": _grew("R6_ui_agent", lambda tool, s: tool == FIRECRAWL,
                         "UI agent: new pages/routes since its last run",
                         focus="pages and client-side views discovered since the previous exploration"),
    "R7_probe": _r7_probe,
}


def select_actions(snap: Snapshot, settings: RuleSettings) -> List[Action]:
    """At most one action per engine; empty list = saturation."""
    actions: Dict[str, Action] = {}
    for rule_id in RULE_ORDER:
        if rule_id in settings.disabled_rules:
            continue
        for tool, state in snap.tools.items():
            engine = engine_of(tool)
            if engine in actions or state.runs >= settings.max_runs_per_tool:
                continue
            action = RULES[rule_id](state, snap, settings)
            if action:
                actions[engine] = action
    return list(actions.values())
