"""Table-driven tests for the rule engine: one Snapshot fixture per rule, plus priorities and ceilings."""
import tempfile

import pytest

from normalization.normalizer import normalize_endpoint
from orchestrator.rules import (FIRECRAWL, HTTP_PROBE, WAPITI, ZAP_AJAX, ZAP_CLIENT, ZAP_TRADITIONAL,
                                RuleSettings, Snapshot, ToolState, build_snapshot, select_actions)
from storage.state_store import StateStore

BASE = ["http://localhost:3000/"]


def state(tool, runs=1, last_status="ok", stored="1:a", current="1:a", frontier=(), failures=0,
          last_seeds=("http://localhost:3000/x",)):
    return ToolState(tool=tool, runs=runs, last_status=last_status if runs else None,
                     last_seeds=list(last_seeds), consecutive_failures=failures,
                     stored_fingerprint=stored if runs else None, current_fingerprint=current,
                     frontier=list(frontier))


def snap(*states, staged=(ZAP_TRADITIONAL, ZAP_CLIENT, WAPITI, FIRECRAWL)):
    return Snapshot(iteration=3, base_seeds=BASE, tools={s.tool: s for s in states}, staged_tools=set(staged))


GREW = dict(stored="1:a", current="2:b", frontier=["http://localhost:3000/new"])

CASES = [
    # (description, states, expected [(tool, rule_id)])
    ("nothing changed -> saturation",
     [state(ZAP_TRADITIONAL), state(ZAP_CLIENT), state(WAPITI), state(FIRECRAWL), state(HTTP_PROBE)], []),
    ("R1 timeout with retries left",
     [state(WAPITI, last_status="timeout", failures=1)], [(WAPITI, "R1_retry")]),
    ("R1 exhausted -> no retry",
     [state(WAPITI, last_status="failed", failures=2)], []),
    ("R2 tool outside the stages never ran",
     [state(WAPITI, runs=0)], []),  # Wapiti is staged -> no R2
    ("R3 browser frontier changed", [state(ZAP_CLIENT, **GREW)], [(ZAP_CLIENT, "R3_browser_spider")]),
    ("R4 link spider frontier changed", [state(ZAP_TRADITIONAL, **GREW)], [(ZAP_TRADITIONAL, "R4_link_spider")]),
    ("R5 wapiti frontier changed", [state(WAPITI, **GREW)], [(WAPITI, "R5_form_crawler")]),
    ("R6 firecrawl frontier changed", [state(FIRECRAWL, **GREW)], [(FIRECRAWL, "R6_ui_agent")]),
    ("R7 unknown-status URLs changed",
     [state(HTTP_PROBE, runs=0, current="3:c", frontier=["http://localhost:3000/a"])], [(HTTP_PROBE, "R7_probe")]),
    ("changed fingerprint but no usable seeds -> nothing",
     [state(ZAP_CLIENT, stored="1:a", current="2:b", frontier=[])], []),
    ("one action per engine: browser spider wins over link spider in the ZAP lane",
     [state(ZAP_TRADITIONAL, **GREW), state(ZAP_CLIENT, **GREW)], [(ZAP_CLIENT, "R3_browser_spider")]),
    ("retry beats frontier growth in the same engine",
     [state(ZAP_TRADITIONAL, last_status="timeout", failures=1, **GREW), state(ZAP_CLIENT, **GREW)],
     [(ZAP_TRADITIONAL, "R1_retry")]),
    ("engines act in parallel",
     [state(ZAP_CLIENT, **GREW), state(WAPITI, **GREW), state(FIRECRAWL, **GREW)],
     [(ZAP_CLIENT, "R3_browser_spider"), (WAPITI, "R5_form_crawler"), (FIRECRAWL, "R6_ui_agent")]),
]


@pytest.mark.parametrize("desc,states,expected", CASES, ids=[c[0] for c in CASES])
def test_rule_table(desc, states, expected):
    actions = select_actions(snap(*states), RuleSettings())
    assert sorted((a.tool, a.rule_id) for a in actions) == sorted(expected)


def test_first_run_rule_for_unstaged_tool():
    actions = select_actions(snap(state(WAPITI, runs=0), staged=(ZAP_TRADITIONAL,)), RuleSettings())
    assert [(a.tool, a.rule_id, a.seeds) for a in actions] == [(WAPITI, "R2_first_run", BASE)]


def test_retry_reuses_last_seeds():
    a = select_actions(snap(state(FIRECRAWL, last_status="timeout", failures=1, last_seeds=["http://x/1"])),
                       RuleSettings())[0]
    assert a.seeds == ["http://x/1"]


def test_ceiling_and_disabled_rules():
    s = snap(state(WAPITI, runs=10, **GREW))
    assert select_actions(s, RuleSettings(max_runs_per_tool=10)) == []
    assert select_actions(snap(state(WAPITI, **GREW)), RuleSettings(disabled_rules={"R5_form_crawler"})) == []


def test_browser_spider_is_configurable():
    s = snap(state(ZAP_AJAX, **GREW), state(ZAP_CLIENT, **GREW))
    actions = select_actions(s, RuleSettings(browser_spider=ZAP_AJAX))
    assert [(a.tool, a.rule_id) for a in actions] == [(ZAP_AJAX, "R3_browser_spider")]


def test_build_snapshot_from_store_detects_growth():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(db_path=f"{tmpdir}/s.db")
        settings = RuleSettings()
        store.insert_or_update_endpoint(normalize_endpoint("http://localhost:3000/alpha", source_tool=WAPITI,
                                                           status_code=200), 1)
        # ZAP traditional ran and saw the frontier as it is now
        store.record_iteration({"iteration": 1, "chosen_tool": ZAP_TRADITIONAL, "endpoints_before": 0,
                                "endpoints_after": 1, "new_endpoints_discovered": 1, "marginal_gain": 1.0,
                                "frontier_fingerprint": store.frontier_fingerprint(ZAP_TRADITIONAL)})
        s1 = build_snapshot(store, [ZAP_TRADITIONAL], settings, BASE, {ZAP_TRADITIONAL}, 2)
        assert select_actions(s1, settings) == []                       # nothing new for it

        store.insert_or_update_endpoint(normalize_endpoint("http://localhost:3000/bravo", source_tool=WAPITI,
                                                           status_code=200), 2)
        s2 = build_snapshot(store, [ZAP_TRADITIONAL], settings, BASE, {ZAP_TRADITIONAL}, 3)
        [a] = select_actions(s2, settings)
        assert a.rule_id == "R4_link_spider" and "http://localhost:3000/bravo" in a.seeds


def test_page_like_filter_for_browser_tools():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(db_path=f"{tmpdir}/s.db")
        for path, ctype in [("/page", "text/html"), ("/api/x", "application/json"), ("/#/login", "")]:
            ep = normalize_endpoint(f"http://localhost:3000{path}", source_tool=ZAP_TRADITIONAL, status_code=200)
            ep["content_type"] = ctype
            store.insert_or_update_endpoint(ep, 1)
        pages = store.get_pending_frontier(ZAP_CLIENT, limit=None, page_like=True)
        assert "http://localhost:3000/api/x" not in pages
        assert {"http://localhost:3000/page", "http://localhost:3000/#/login"} <= set(pages)
