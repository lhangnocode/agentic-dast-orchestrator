"""
Target-agnostic surface signals.

`classify_surface` turns raw surface counts into signals (SPA likelihood, auth walls) that are
recorded with every rule decision. Tool selection itself lives in orchestrator/rules.py.
"""

from typing import Any, Dict, List

AUTH_WALL_STATUSES = (301, 302, 303, 307, 308, 401, 403)


def classify_surface(profile: Dict[str, int], html_page_max: int = 3) -> Dict[str, Any]:
    """
    Generic SPA / auth-wall detection from the observed surface (binary signals):
    - hash_routes: `/#/x` routes exist - only client-side routers produce them;
    - js_shell: the app answers JSON but serves at most `html_page_max` HTML pages, so the UI
      is rendered in the browser (threshold is configurable; calibrate on benchmarks);
    - api_heavy: at least as many API-like endpoints as HTML pages (and at least 3).
    """
    signals: List[str] = []
    html, api = profile.get("html_pages", 0), profile.get("api_like", 0)
    if profile.get("hash_routes", 0) > 0:
        signals.append("hash_routes")
    if profile.get("json_responses", 0) > 0 and html <= html_page_max:
        signals.append("js_shell")
    if api >= 3 and api >= html:
        signals.append("api_heavy")

    spa = "high" if len(signals) >= 2 else "medium" if signals else "low"
    walls = profile.get("auth_walls", 0) + profile.get("redirects", 0)
    return {"spa_likelihood": spa, "spa_signals": signals, "auth_walls_or_redirects": walls}
